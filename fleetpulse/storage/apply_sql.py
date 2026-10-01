"""Apply a SQL file (migrations / optimisation) using DATABASE_URL.  python -m fleetpulse.storage.apply_sql db/optimize.sql"""
import os
import sys

import psycopg

if __name__ == "__main__":
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        for path in sys.argv[1:]:
            c.execute(open(path).read())
            print("applied", path)
