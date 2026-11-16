#!/bin/bash
# Runs once, the first time the Postgres container starts with an empty volume.
# Creates the warranty tables plus everything Estuary's Postgres capture needs.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -v flow_pw="$FLOW_CAPTURE_PASSWORD" <<'EOSQL'

-- ---------------------------------------------------------------
-- Source tables (fake data only)
-- ---------------------------------------------------------------
CREATE TABLE public.vehicles (
    vin         TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    model_year  INT  NOT NULL,
    market      TEXT NOT NULL,
    sold_on     DATE NOT NULL
);

CREATE TABLE public.warranty_claims (
    claim_id      BIGSERIAL PRIMARY KEY,
    vin           TEXT NOT NULL REFERENCES public.vehicles (vin),
    model         TEXT NOT NULL,        -- denormalised to keep downstream queries simple
    component     TEXT NOT NULL,
    dealer_code   TEXT NOT NULL,
    mileage_km    INT  NOT NULL,
    claim_amount  INT  NOT NULL,        -- whole euros; INT (not NUMERIC) so it arrives as a JSON number
    status        TEXT NOT NULL DEFAULT 'submitted',
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------
-- Estuary capture prerequisites
-- (per Estuary's PostgreSQL connector docs)
-- ---------------------------------------------------------------

-- Dedicated, least-privilege user for the capture
CREATE USER flow_capture WITH PASSWORD :'flow_pw' REPLICATION;
GRANT pg_read_all_data TO flow_capture;   -- Postgres 14+

-- Watermarks table: Estuary uses this to line up backfills with the live change stream
CREATE TABLE IF NOT EXISTS public.flow_watermarks (slot TEXT PRIMARY KEY, watermark TEXT);
GRANT ALL PRIVILEGES ON TABLE public.flow_watermarks TO flow_capture;

-- Publication: the set of tables whose changes are streamed
CREATE PUBLICATION flow_publication;
ALTER PUBLICATION flow_publication SET (publish_via_partition_root = true);
ALTER PUBLICATION flow_publication
    ADD TABLE public.flow_watermarks, public.vehicles, public.warranty_claims;

EOSQL

echo "warranty database initialised for Estuary CDC"
