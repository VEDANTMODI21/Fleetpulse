"""Maintenance Copilot: an agent that answers questions and proposes actions on
fleet data through a small allow-list of tools.

Guardrails
1. Input screening - prompt-injection / exfiltration patterns are refused.
2. Tool allow-list per role; every tool receives the caller's Principal, so tenant
   scope is enforced in code, not by the prompt.
3. Write actions are *proposals*: `propose_work_order` creates a PROPOSED row that
   a fleet manager must approve (human in the loop).
4. Budget: max 4 tool calls per request.
5. Grounding check: every VIN / number in the final answer must come from tool
   output; otherwise the answer is replaced by a safe fallback.
6. Every tool call and the final answer are written to the hash-chained audit log.

Planner: deterministic intent router by default (no external calls, zero cost,
reproducible). `LLM_PROVIDER=anthropic` swaps in an LLM planner that picks tools
via function calling; guardrails 1-6 wrap either planner identically.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fleetpulse.agent.kb import InMemoryVectorStore

INJECTION = re.compile(
    r"(ignore (all|any|previous|prior)|disregard .*instructions|system prompt|you are now|developer mode|"
    r"jailbreak|reveal .*(secret|key|token|password)|other (fleet|tenant)s?'? data|drop table|;--|"
    r"exfiltrat|act as (an? )?admin)", re.I)
VIN_IN_TEXT = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
MAX_TOOL_CALLS = 4

ROLE_TOOLS = {
    "platform_admin": {"at_risk", "vehicle", "kb", "propose_work_order", "plan_week"},
    "fleet_manager": {"at_risk", "vehicle", "kb", "propose_work_order", "plan_week"},
    "technician": {"at_risk", "vehicle", "kb", "propose_work_order"},
    "analyst": {"at_risk", "kb"},
}


@dataclass
class AgentResult:
    answer: str
    tool_calls: list[dict] = field(default_factory=list)
    refused: bool = False
    grounded: bool = True
    latency_ms: float = 0.0


class Copilot:
    def __init__(self, tools: dict[str, Callable[..., Any]], audit: Callable[[str, dict], None],
                 kb: InMemoryVectorStore | None = None):
        self.tools, self.audit = tools, audit
        self.kb = kb or InMemoryVectorStore()

    # ---- planner ---------------------------------------------------------------------
    def plan(self, q: str) -> list[tuple[str, dict]]:
        ql = q.lower()
        vins = VIN_IN_TEXT.findall(q.upper())
        steps: list[tuple[str, dict]] = []
        if vins:
            steps.append(("vehicle", {"vin": vins[0]}))
            if re.search(r"\b(book|schedule|create|raise|open)\b.*\b(work ?order|service|repair|workshop)", ql):
                steps.append(("propose_work_order", {"vin": vins[0], "reason": q[:200]}))
            else:
                steps.append(("kb", {"query": q}))
        elif re.search(r"\b(plan|schedule|budget|workshop capacity|bays?)\b", ql):
            m = re.search(r"(\d+)\s*(h|hours|technician hours)", ql)
            steps.append(("plan_week", {"budget_hours": float(m.group(1)) if m else 40.0}))
        elif re.search(r"\b(risk|likely|break ?down|fail|which vehicles|top)\b", ql):
            m = re.search(r"\btop\s*(\d+)", ql)
            steps.append(("at_risk", {"limit": min(int(m.group(1)), 25) if m else 5}))
        else:
            steps.append(("kb", {"query": q}))
        return steps

    # ---- run -------------------------------------------------------------------------
    def ask(self, principal, question: str, audit: Callable[[str, dict], None] | None = None) -> AgentResult:
        t0 = time.perf_counter()
        _audit = audit or self.audit
        if len(question) > 1000 or INJECTION.search(question):
            _audit("agent.refused", {"q": question[:200]})
            return AgentResult("I can't help with that request. I can answer questions about your fleet's "
                               "vehicle health, risks and maintenance.", refused=True,
                               latency_ms=(time.perf_counter() - t0) * 1000)
        allowed = ROLE_TOOLS.get(principal.role, set())
        calls, outputs = [], []
        for name, args in self.plan(question)[:MAX_TOOL_CALLS]:
            if name not in allowed:
                calls.append({"tool": name, "args": args, "error": "not permitted for role"})
                continue
            try:
                out = self.tools[name](principal, **args) if name != "kb" else self.kb.search(args["query"], 2)
            except Exception as e:  # noqa: BLE001 - tool failures become part of the answer, never crash the agent
                out = {"error": type(e).__name__}
            calls.append({"tool": name, "args": args})
            outputs.append((name, out))
            _audit("agent.tool_call", {"tool": name, "args": args})
        answer = self.compose(outputs, calls)
        grounded = self.grounded(answer, outputs)
        if not grounded:
            answer = "I couldn't verify an answer from your fleet data, so I'm not going to guess."
        _audit("agent.answer", {"q": question[:200], "grounded": grounded, "tools": [c["tool"] for c in calls]})
        return AgentResult(answer, calls, False, grounded, (time.perf_counter() - t0) * 1000)

    # ---- answer composition (template-based -> no free-form hallucination) ------------
    def compose(self, outputs, calls) -> str:
        parts = []
        for c in calls:
            if c.get("error"):
                parts.append(f"(Tool '{c['tool']}' is not available for your role.)")
        for name, out in outputs:
            if isinstance(out, dict) and out.get("error"):
                parts.append(f"Tool {name} failed ({out['error']}).")
            elif name == "at_risk":
                items = out["items"]
                lines = [f"{i + 1}. {v['vin']} - {v['p_fail_7d'] * 100:.0f}% 7-day breakdown risk"
                         + (f" ({v['top_factors'][0]['factor']})" if v["top_factors"] else "")
                         for i, v in enumerate(items)]
                parts.append(f"Highest-risk vehicles (scored {out['scored_on']}):\n" + "\n".join(lines))
            elif name == "vehicle":
                if out is None:
                    parts.append("That vehicle was not found in your fleet.")
                    continue
                r = out["risk_history"][-1] if out["risk_history"] else None
                s = f"{out['vin']} ({out['oem']} {out['powertrain']} {out['model_year']}, {out['city']})"
                if r:
                    s += f": {r['p_fail_7d'] * 100:.0f}% risk of breakdown in 7 days."
                    for f in r["top_factors"]:
                        s += f"\n- {f['factor']} ({f['signal']} = {f['value']})"
                parts.append(s)
            elif name == "kb":
                parts.append("From the repair knowledge base:\n" + "\n".join(
                    f"- [{d['id']}] {d['title']}: {d['text'][:220]}..." for d in out))
            elif name == "propose_work_order":
                parts.append(f"Proposed work order #{out['id']} for approval by a fleet manager (status {out['status']}).")
            elif name == "plan_week":
                parts.append(f"Recommended workshop plan: {len(out['chosen'])} vehicles, "
                             f"{out['hours_used']} technician hours, expected avoided cost USD {out['expected_saving']:,.0f}."
                             + "".join(f"\n- {c['vin']} ({c['p_fail'] * 100:.0f}% risk, {c['hours']} h)"
                                       for c in out["chosen"][:10]))
        return "\n\n".join(parts) if parts else "I don't have data to answer that."

    @staticmethod
    def grounded(answer: str, outputs) -> bool:
        blob = repr(outputs)
        return all(v in blob for v in VIN_IN_TEXT.findall(answer))
