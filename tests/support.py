"""Shared test-environment bootstrap (used by pytest integration fixtures and behave BDD)."""
import os
import subprocess
import sys

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def prepare(data_dir: str, dsn: str, redis_url: str, vehicles: int = 1500, days: int = 45, db="fleetpulse_test"):
    admin = dsn.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {db}")
    env = {**os.environ, "DATABASE_URL": dsn, "REDIS_URL": redis_url, "DATA_DIR": data_dir, "PYTHONPATH": ROOT}
    subprocess.run([sys.executable, "-c",
                    f"from fleetpulse.simulator.seed import main; main(['--vehicles','{vehicles}','--days','{days}'])"],
                   env=env, check=True, cwd=ROOT)
    subprocess.run([sys.executable, "-m", "fleetpulse.storage.seed_pg", "--data", data_dir, "--hot-days", "20",
                    "--models", os.path.join(ROOT, "models")], env=env, check=True, cwd=ROOT)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(open(os.path.join(ROOT, "db/optimize.sql")).read())
