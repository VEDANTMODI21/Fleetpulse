"""VIN validation (ISO 3779 / NHTSA 49 CFR 565) and generation.

Time: O(17) = O(1) per VIN.  Space: O(1).
"""
from __future__ import annotations

import random
import re

# 17 chars, digits + capitals, excluding I, O, Q.
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

_TRANSLIT = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"


def check_digit(vin: str) -> str:
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS))
    r = total % 11
    return "X" if r == 10 else str(r)


def is_valid_vin(vin: str) -> bool:
    """Syntax + check digit (position 9)."""
    if not isinstance(vin, str) or not VIN_RE.match(vin):
        return False
    return vin[8] == check_digit(vin)


def make_vin(rng: random.Random, wmi: str = "1HG") -> str:
    body = wmi + "".join(rng.choice(_ALPHABET) for _ in range(14))
    body = body[:8] + "0" + body[9:]
    return body[:8] + check_digit(body) + body[9:]
