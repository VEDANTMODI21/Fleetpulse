"""Model registry helpers: models are saved with a SHA-256 sidecar and only
loaded if the digest matches (defends against a tampered pickle - pickle
executes code on load, so artefacts must come from the trusted CI pipeline)."""
from __future__ import annotations

import hashlib
import pickle  # noqa: S403 - integrity-checked below


class ModelIntegrityError(RuntimeError):
    pass


def save_model(obj, path: str) -> str:
    blob = pickle.dumps(obj)
    digest = hashlib.sha256(blob).hexdigest()
    with open(path, "wb") as fh:
        fh.write(blob)
    with open(path + ".sha256", "w") as fh:
        fh.write(digest)
    return digest


def load_model(path: str):
    with open(path, "rb") as fh:
        blob = fh.read()
    try:
        expected = open(path + ".sha256").read().strip()
    except FileNotFoundError as e:
        raise ModelIntegrityError("missing digest") from e
    if hashlib.sha256(blob).hexdigest() != expected:
        raise ModelIntegrityError("model digest mismatch")
    return pickle.loads(blob)  # noqa: S301 - verified artefact
