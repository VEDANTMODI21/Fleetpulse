import random

import pytest

from fleetpulse.algorithms.dtc import parse_dtcs
from fleetpulse.algorithms.vin import check_digit, is_valid_vin, make_vin


def test_known_valid_vin():
    assert is_valid_vin("1M8GDM9AXKP042788")          # NHTSA reference example (check digit X)
    assert check_digit("1M8GDM9AXKP042788") == "X"


@pytest.mark.parametrize("vin", ["1M8GDM9A1KP042788",      # wrong check digit
                                 "1M8GDM9AXKP04278",       # 16 chars
                                 "1M8GDM9AXKP04278I",      # contains I
                                 "1M8GDM9AXKP0427O8",      # contains O
                                 "1m8gdm9axkp042788",      # lower case
                                 None, 12345])
def test_invalid_vins(vin):
    assert not is_valid_vin(vin)


def test_generated_vins_are_valid_and_unique():
    rng = random.Random(0)
    vins = {make_vin(rng) for _ in range(5000)}
    assert len(vins) == 5000 and all(is_valid_vin(v) for v in vins)


@pytest.mark.parametrize("raw,expected", [
    ("P0301", ["P0301"]), ("p0301", ["P0301"]), ("DTC=P0301;P0420", ["P0301", "P0420"]),
    (["P0217", "P0217"], ["P0217"]), ("0x0301 0x0420", ["P0301", "P0420"]), ("0x4123", ["C0123"]),
    ("P03-01", ["P0301"]), ("no codes here", []), (None, []), ("", []), ("U0100 B1000", ["U0100", "B1000"]),
])
def test_parse_dtcs(raw, expected):
    assert [d.code for d in parse_dtcs(raw)] == expected


def test_dtc_attributes():
    d = parse_dtcs("P0301 P1234 U0100")
    assert d[0].severe and d[0].generic and d[0].system == "powertrain"
    assert not d[1].generic
    assert d[2].system == "network" and not d[2].severe
