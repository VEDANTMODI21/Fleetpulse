"""OBD-II Diagnostic Trouble Code parsing (SAE J2012).

Raw OEM payloads encode DTCs inconsistently: "P0301", "p0301", "P03-01",
hex words like "0x0301" (J1979 two-byte format), or free text
"DTC=P0301;P0420".  We normalise all of them to canonical 5-char codes.

Time: O(n) in payload length (single regex scan). Space: O(k) codes found.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DTC_RE = re.compile(r"\b([PCBU])\s*([0-3])([0-9A-F])\s*-?\s*([0-9A-F]{2})\b", re.IGNORECASE)
HEX_RE = re.compile(r"\b0x([0-9A-F]{4})\b", re.IGNORECASE)
_SYSTEM = {"P": "powertrain", "C": "chassis", "B": "body", "U": "network"}
_HEX_PREFIX = "PCBU"

# Subsystem hints used for severity + feature engineering.
SEVERE_PREFIXES = ("P030", "P0217", "P0A", "P0562", "P0118", "P0128", "P0335", "P0700")


@dataclass(frozen=True)
class Dtc:
    code: str
    system: str
    generic: bool  # SAE-defined (0/2) vs manufacturer-specific (1/3)

    @property
    def severe(self) -> bool:
        return self.code.startswith(SEVERE_PREFIXES)


def _from_hex(word: str) -> str:
    v = int(word, 16)
    return f"{_HEX_PREFIX[(v >> 14) & 0x3]}{(v >> 12) & 0x3}{(v >> 8) & 0xF:X}{v & 0xFF:02X}"


def parse_dtcs(raw) -> list[Dtc]:
    """Accepts str, list[str] or None; returns de-duplicated, ordered codes."""
    if raw is None:
        return []
    text = " ".join(map(str, raw)) if isinstance(raw, (list, tuple)) else str(raw)
    codes: list[str] = []
    for m in DTC_RE.finditer(text):
        codes.append(f"{m.group(1).upper()}{m.group(2)}{m.group(3).upper()}{m.group(4).upper()}")
    for m in HEX_RE.finditer(text):
        codes.append(_from_hex(m.group(1)))
    seen: dict[str, None] = dict.fromkeys(codes)
    return [Dtc(c, _SYSTEM[c[0]], c[1] in "02") for c in seen]
