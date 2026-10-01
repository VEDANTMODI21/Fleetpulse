import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)

from tests.support import prepare  # noqa: E402


def before_all(context):
    dsn = os.environ.get("BDD_DATABASE_URL", "postgresql://fleet@localhost:5432/fleetpulse_bdd")
    rurl = os.environ.get("BDD_REDIS_URL", "redis://localhost:6379/2")
    prepare(tempfile.mkdtemp(), dsn, rurl, db=dsn.rsplit("/", 1)[1])
    os.environ["DATABASE_URL"], os.environ["REDIS_URL"] = dsn, rurl
    import redis
    from fastapi.testclient import TestClient
    from psycopg_pool import ConnectionPool

    from fleetpulse.api.app import create_app, reset_conn
    context.pool = ConnectionPool(dsn, min_size=1, max_size=4, reset=reset_conn, open=True)
    context.redis = redis.Redis.from_url(rurl)
    context.redis.flushdb()
    context.client = TestClient(create_app(context.pool, context.redis)).__enter__()


def after_all(context):
    context.client.__exit__(None, None, None)
    context.pool.close()
