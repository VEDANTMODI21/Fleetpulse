"""Integration fixtures: real PostgreSQL 16 + Redis 7.

* USE_TESTCONTAINERS=1 -> containers are started with testcontainers-python (CI / laptops with Docker).
* otherwise TEST_DATABASE_URL / TEST_REDIS_URL point at running services (GitHub Actions `services:` or local).
A small synthetic dataset (1,500 vehicles x 45 days) is generated, loaded and scored once per session.
"""
import os

import pytest

from tests.support import prepare


def _containers():
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer
    pg = PostgresContainer("postgres:16-alpine").start()
    rd = RedisContainer("redis:7-alpine").start()
    return (pg.get_connection_url().replace("postgresql+psycopg2", "postgresql"),
            f"redis://{rd.get_container_host_ip()}:{rd.get_exposed_port(6379)}/1", (pg, rd))


@pytest.fixture(scope="session")
def services(tmp_path_factory):
    handles = None
    if os.environ.get("USE_TESTCONTAINERS") == "1":
        dsn, rurl, handles = _containers()
    else:
        dsn = os.environ.get("TEST_DATABASE_URL", "postgresql://fleet@localhost:5432/fleetpulse_test")
        rurl = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")
    data = tmp_path_factory.mktemp("data")
    prepare(str(data), dsn, rurl, db=dsn.rsplit("/", 1)[1])
    os.environ["DATABASE_URL"], os.environ["REDIS_URL"] = dsn, rurl
    yield {"dsn": dsn, "redis": rurl, "data": str(data)}
    if handles:
        for h in handles:
            h.stop()


@pytest.fixture(scope="session")
def pool(services):
    from psycopg_pool import ConnectionPool

    from fleetpulse.api.app import reset_conn
    p = ConnectionPool(services["dsn"], min_size=1, max_size=8, reset=reset_conn, open=True)
    yield p
    p.close()


@pytest.fixture(scope="session")
def rclient(services):
    import redis
    r = redis.Redis.from_url(services["redis"])
    r.flushdb()
    return r
