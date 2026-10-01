"""Repair knowledge base + vector store port.

Embeddings: hashed word+char-n-gram TF vectors (512-d, L2-normalised) - fully
local, deterministic, no data leaves the tenant. In production the same
VectorStore port is backed by pgvector (`kb_chunk.embedding vector(512)`,
HNSW index, cosine ops); locally an in-memory numpy matrix is used.
"""
from __future__ import annotations

import re
from typing import Protocol

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

DOCS = [
    ("KB-001", "P0217 engine coolant over-temperature", "Coolant over-temperature (P0217). Likely causes: failing water pump, stuck-closed thermostat, clogged radiator, low coolant from leak, failed cooling fan. Action: stop vehicle if gauge in red, pressure-test cooling system, inspect water pump weep hole, replace thermostat. Typical labour 2.5 h. Risk if ignored: head gasket failure, engine seizure, roadside breakdown."),
    ("KB-002", "P0128 thermostat below regulating temperature", "Thermostat stuck open (P0128): engine slow to warm, poor fuel economy. Replace thermostat and gasket, bleed coolant. Labour 1.5 h. Low breakdown risk alone but combined with rising coolant max indicates cooling system degradation."),
    ("KB-003", "P0562 system voltage low 12V battery", "Low 12 V system voltage (P0562). Causes: ageing auxiliary battery, weak alternator, parasitic drain, corroded terminals. Test battery state of health and alternator output (13.8-14.4 V). Replace battery if resting voltage below 12.2 V or cranking below 9.6 V. Labour 0.5 h. No-start breakdown risk rises sharply below 11.8 V minimum."),
    ("KB-004", "P0300 P0301 P0302 misfire", "Cylinder misfire (P0300 random, P0301/P0302 cylinder-specific). Causes: worn spark plugs, failing ignition coil, injector fault, vacuum leak, low compression. Swap coil to confirm, replace plugs and coil. Labour 1.5 h. Sustained misfire damages the catalytic converter and can cause limp-home mode."),
    ("KB-005", "P0520 P0524 engine oil pressure low", "Low engine oil pressure (P0524) or sensor circuit (P0520). Check oil level first; inspect for leaks at sump gasket and filter; verify with mechanical gauge; worn oil pump or bearings. Stop driving if pressure warning lamp is lit. Labour 3 h. Ignoring leads to catastrophic engine failure."),
    ("KB-006", "P0A80 replace hybrid EV battery pack", "HV battery pack degradation (P0A80): cell voltage or temperature imbalance, rising energy consumption per 100 km, reduced range. Run cell balance diagnostics, inspect battery cooling loop and fans, replace failing modules. Labour 4 h. Risk: vehicle enters reduced-power mode or will not start."),
    ("KB-007", "P0AFA hybrid battery system voltage low", "HV battery system voltage low (P0AFA). Check HV contactors, battery management system logs, module voltages. Often precedes P0A80. Labour 2 h."),
    ("KB-008", "P0118 coolant temperature sensor circuit high", "Coolant temperature sensor circuit high (P0118). Frequently an intermittent sensor or connector glitch, not an actual overheat. Verify against infrared thermometer; inspect connector. Low urgency unless paired with rising coolant temperature trend."),
    ("KB-009", "P0420 catalyst efficiency", "Catalyst efficiency below threshold (P0420). Emissions issue; not a breakdown risk in the short term. Schedule at next routine service."),
    ("KB-010", "P0455 P0442 evap leak", "Evaporative emission leak (P0455 large, P0442 small). Often a loose fuel cap. No breakdown risk. Tighten or replace fuel cap, smoke-test EVAP system at next service."),
    ("KB-011", "U0100 lost communication ECM", "Lost communication with ECM (U0100). Check CAN bus wiring, ground straps, ECM power supply. Can be intermittent due to low battery voltage."),
    ("KB-012", "Workshop scheduling policy", "Fleet policy: vehicles with 7-day failure risk above 50% are pulled into the workshop within 48 hours; 15-50% are scheduled at the next available bay this week; below 15% continue normal service intervals. Work orders created by the copilot are PROPOSED and require fleet manager approval."),
    ("KB-013", "Cost of breakdown vs planned repair", "Average unplanned roadside breakdown cost: towing USD 250, 2.5 days downtime at USD 180/day, repair premium 30%. A planned repair avoids towing and cuts downtime to 0.5 day. Expected saving per prevented breakdown is roughly USD 900-1,400 depending on fault."),
    ("KB-014", "Harsh braking and brake wear", "High harsh-braking counts accelerate brake pad and rotor wear and correlate with higher fuel consumption. Coach drivers with more than 5 harsh events per 100 km."),
]


def _tokenize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", text.lower())


class VectorStore(Protocol):
    def search(self, query: str, k: int = 3) -> list[dict]: ...


class InMemoryVectorStore:
    def __init__(self, docs=DOCS, dims: int = 512):
        self.vec = HashingVectorizer(n_features=dims, analyzer="char_wb", ngram_range=(3, 5), norm="l2",
                                     alternate_sign=False, preprocessor=_tokenize)
        self.docs = [{"id": i, "title": t, "text": x} for i, t, x in docs]
        self.M = self.vec.transform([d["title"] + " " + d["text"] for d in self.docs]).toarray().astype(np.float32)

    def embed(self, text: str) -> np.ndarray:
        return self.vec.transform([text]).toarray()[0].astype(np.float32)

    def search(self, query: str, k: int = 3) -> list[dict]:
        q = self.embed(query)
        sims = self.M @ q
        idx = np.argsort(-sims)[:k]
        return [{**self.docs[i], "score": round(float(sims[i]), 3)} for i in idx]


def keyword_search(query: str, k: int = 3) -> list[str]:
    """Baseline: exact keyword overlap count."""
    q = set(_tokenize(query).split())
    scored = sorted(DOCS, key=lambda d: -len(q & set(_tokenize(d[1] + " " + d[2]).split())))
    return [d[0] for d in scored[:k]]


EVAL_QUERIES = [
    ("engine running hot coolant temperature rising", "KB-001"), ("truck overheating on highway", "KB-001"),
    ("battery voltage low wont start", "KB-003"), ("12v battery weak", "KB-003"), ("alternator charging problem", "KB-003"),
    ("engine shaking misfiring cylinder 2", "KB-004"), ("rough idle ignition coil", "KB-004"),
    ("oil pressure warning light", "KB-005"), ("oil leak low pressure", "KB-005"),
    ("EV range dropping cell imbalance", "KB-006"), ("hybrid battery degradation", "KB-006"),
    ("temperature sensor glitch false alarm", "KB-008"), ("catalytic converter code", "KB-009"),
    ("fuel cap loose evap", "KB-010"), ("CAN bus communication lost", "KB-011"),
    ("when should risky vehicles go to workshop", "KB-012"), ("how much does a breakdown cost", "KB-013"),
    ("driver brakes too hard", "KB-014"), ("thermostat stuck open slow warm up", "KB-002"),
    ("HV contactor voltage low", "KB-007"),
]


def evaluate_retrieval(k: int = 3) -> dict:
    store = InMemoryVectorStore()
    hit_v = sum(any(d["id"] == gold for d in store.search(q, k)) for q, gold in EVAL_QUERIES)
    hit_k = sum(gold in keyword_search(q, k) for q, gold in EVAL_QUERIES)
    top1_v = sum(store.search(q, 1)[0]["id"] == gold for q, gold in EVAL_QUERIES)
    return {"queries": len(EVAL_QUERIES), f"vector_recall@{k}": hit_v / len(EVAL_QUERIES),
            f"keyword_recall@{k}": hit_k / len(EVAL_QUERIES), "vector_top1": top1_v / len(EVAL_QUERIES)}
