import pytest

from fleetpulse.ml.registry import ModelIntegrityError, load_model, save_model


def test_model_roundtrip_and_tamper_detection(tmp_path):
    p = str(tmp_path / "m.pkl")
    save_model({"w": [1, 2, 3]}, p)
    assert load_model(p) == {"w": [1, 2, 3]}
    with open(p, "ab") as fh:
        fh.write(b"evil")
    with pytest.raises(ModelIntegrityError):
        load_model(p)
    (tmp_path / "nodigest.pkl").write_bytes(b"x")
    with pytest.raises(ModelIntegrityError):
        load_model(str(tmp_path / "nodigest.pkl"))
