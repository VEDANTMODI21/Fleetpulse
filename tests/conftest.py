import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for name in ("JWT_SECRET", "DEMO_PASSWORD", "INGEST_KEY_ALPHA", "INGEST_KEY_BETA", "INGEST_KEY_GAMMA"):
	os.environ.setdefault(name, secrets.token_urlsafe(32))
