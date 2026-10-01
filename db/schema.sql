-- FleetPulse relational core (PostgreSQL 16). 3NF except where noted (DENORM).
-- CP store: ownership, subscriptions, access control, alerts, work orders, audit.

CREATE TABLE IF NOT EXISTS fleet (
  id            SERIAL PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  segment       TEXT NOT NULL CHECK (segment IN ('rental','logistics','ride_hailing','lender','dealer')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subscription (
  id            SERIAL PRIMARY KEY,
  fleet_id      INT NOT NULL REFERENCES fleet(id),
  plan          TEXT NOT NULL CHECK (plan IN ('basic','pro','enterprise')),
  vehicle_limit INT NOT NULL CHECK (vehicle_limit > 0),
  valid_from    DATE NOT NULL,
  valid_to      DATE,
  CHECK (valid_to IS NULL OR valid_to > valid_from)
);

CREATE TABLE IF NOT EXISTS app_user (
  id            SERIAL PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE,
  fleet_id      INT REFERENCES fleet(id),          -- NULL = platform operator
  role          TEXT NOT NULL CHECK (role IN ('platform_admin','fleet_manager','technician','analyst'))
);

CREATE TABLE IF NOT EXISTS oem (
  id            SMALLSERIAL PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL
);

-- Declarative OEM payload mappings: onboard a new OEM without redeploying (DeclarativeAdapter).
CREATE TABLE IF NOT EXISTS oem_mapping (
  oem_id        SMALLINT NOT NULL REFERENCES oem(id),
  version       INT NOT NULL,
  mapping       JSONB NOT NULL,
  active        BOOLEAN NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (oem_id, version)
);

CREATE TABLE IF NOT EXISTS vehicle_model (
  id            SERIAL PRIMARY KEY,
  oem_id        SMALLINT NOT NULL REFERENCES oem(id),
  name          TEXT NOT NULL,
  powertrain    TEXT NOT NULL CHECK (powertrain IN ('ICE','HYBRID','EV')),
  model_year    SMALLINT NOT NULL,
  UNIQUE (oem_id, name, model_year)
);

CREATE TABLE IF NOT EXISTS depot (
  id            SERIAL PRIMARY KEY,
  fleet_id      INT NOT NULL REFERENCES fleet(id),
  name          TEXT NOT NULL,
  city          TEXT NOT NULL,
  lat           DOUBLE PRECISION NOT NULL,
  lon           DOUBLE PRECISION NOT NULL,
  service_bays  SMALLINT NOT NULL DEFAULT 4
);

CREATE TABLE IF NOT EXISTS vehicle (
  id            SERIAL PRIMARY KEY,
  vin           CHAR(17) NOT NULL UNIQUE CHECK (vin ~ '^[A-HJ-NPR-Z0-9]{17}$'),
  fleet_id      INT NOT NULL REFERENCES fleet(id),
  model_id      INT NOT NULL REFERENCES vehicle_model(id),
  home_depot_id INT REFERENCES depot(id),
  onboarded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS driver (
  id            SERIAL PRIMARY KEY,
  fleet_id      INT NOT NULL REFERENCES fleet(id),
  pseudonym     TEXT NOT NULL,              -- no real names stored (privacy by design)
  licence_hash  BYTEA NOT NULL,             -- salted SHA-256 of licence no.
  erased_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS vehicle_assignment (
  vehicle_id    INT NOT NULL REFERENCES vehicle(id),
  driver_id     INT NOT NULL REFERENCES driver(id),
  from_ts       TIMESTAMPTZ NOT NULL,
  to_ts         TIMESTAMPTZ,
  PRIMARY KEY (vehicle_id, from_ts)
);

CREATE TABLE IF NOT EXISTS trip (
  id            BIGSERIAL PRIMARY KEY,
  vehicle_id    INT NOT NULL REFERENCES vehicle(id),
  driver_id     INT REFERENCES driver(id),
  start_ts      TIMESTAMPTZ NOT NULL,
  end_ts        TIMESTAMPTZ,
  distance_km   REAL
);

CREATE TABLE IF NOT EXISTS dtc_catalog (
  code          CHAR(5) PRIMARY KEY,
  system        TEXT NOT NULL,
  description   TEXT NOT NULL,
  severity      TEXT NOT NULL CHECK (severity IN ('INFO','WARN','CRITICAL'))
);

-- DENORM: alert.fleet_id duplicates vehicle.fleet_id so tenant filters and
-- row-level security run without a join on the hottest dashboard path.
-- Kept consistent by trigger-free design: alerts are immutable and vehicles never change fleet
-- (a transfer creates a new vehicle row).
CREATE TABLE IF NOT EXISTS alert (
  id            BIGSERIAL PRIMARY KEY,
  vehicle_id    INT NOT NULL REFERENCES vehicle(id),
  fleet_id      INT NOT NULL REFERENCES fleet(id),
  ts            TIMESTAMPTZ NOT NULL,
  kind          TEXT NOT NULL,
  severity      TEXT NOT NULL CHECK (severity IN ('INFO','WARN','CRITICAL')),
  detail        TEXT NOT NULL,
  value         REAL,
  status        TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACK','CLOSED')),
  dedup_key     TEXT UNIQUE                -- idempotent insert from at-least-once stream
);

CREATE TABLE IF NOT EXISTS risk_score (
  vehicle_id    INT NOT NULL REFERENCES vehicle(id),
  scored_on     DATE NOT NULL,
  p_fail_7d     REAL NOT NULL CHECK (p_fail_7d BETWEEN 0 AND 1),
  model_version TEXT NOT NULL,
  top_factors   JSONB NOT NULL DEFAULT '[]',
  PRIMARY KEY (vehicle_id, scored_on)
);

CREATE TABLE IF NOT EXISTS work_order (
  id            BIGSERIAL PRIMARY KEY,
  vehicle_id    INT NOT NULL REFERENCES vehicle(id),
  depot_id      INT REFERENCES depot(id),
  reason        TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'PROPOSED' CHECK (status IN ('PROPOSED','APPROVED','REJECTED','DONE')),
  proposed_by   TEXT NOT NULL,             -- user email or 'agent:copilot'
  approved_by   TEXT,
  scheduled_for DATE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  idempotency_key TEXT UNIQUE
);

-- Daily telemetry rollups: hot tier (last 30-90 days). Partitioned by month.
CREATE TABLE IF NOT EXISTS vehicle_daily (
  vehicle_id    INT NOT NULL,
  day           DATE NOT NULL,
  km            REAL, engine_hours REAL, idle_hours REAL, harsh_brakes REAL,
  coolant_max   REAL, aux_v_min REAL, oil_kpa_min REAL, misfire_cnt REAL,
  soc_drop_per100 REAL, cell_temp_spread REAL,
  dtc_cnt       SMALLINT NOT NULL DEFAULT 0,
  severe_dtc_cnt SMALLINT NOT NULL DEFAULT 0,
  dtcs          TEXT,
  breakdown     BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY (vehicle_id, day)
) PARTITION BY RANGE (day);

-- Hash-chained, append-only audit log (tamper evident). Every data access + agent action.
CREATE TABLE IF NOT EXISTS audit_log (
  id            BIGSERIAL PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor         TEXT NOT NULL,
  fleet_id      INT,
  action        TEXT NOT NULL,
  resource      TEXT NOT NULL,
  detail        JSONB NOT NULL DEFAULT '{}',
  prev_hash     TEXT NOT NULL,
  hash          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS erasure_request (
  id            SERIAL PRIMARY KEY,
  subject_type  TEXT NOT NULL CHECK (subject_type IN ('driver')),
  subject_id    INT NOT NULL,
  requested_by  TEXT NOT NULL,
  requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at  TIMESTAMPTZ
);
