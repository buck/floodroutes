-- houston311 schema for floodroutes. Idempotent: safe to run on every load.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;

-- One row per loaded source file. Raw rows are kept only for the latest
-- snapshot of each source stem (daily YTD refreshes would otherwise pile up);
-- this log keeps the provenance of every snapshot ever loaded.
CREATE TABLE IF NOT EXISTS raw.load_log (
    source_file      text PRIMARY KEY,
    source_stem      text NOT NULL,
    source_sha256    text NOT NULL,
    extract_ts       timestamptz,
    header           text[] NOT NULL,
    records          integer,
    parse_ok         integer,
    parse_extra      integer,
    parse_short      integer,
    loaded_at        timestamptz NOT NULL DEFAULT now(),
    core_upserted    integer,
    core_upserted_at timestamptz
);

-- All source columns as text. Columns are the union of every schema seen;
-- load.py adds any new header column with ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS raw.service_request_line (
    source_file   text NOT NULL REFERENCES raw.load_log ON DELETE CASCADE,
    source_stem   text NOT NULL,
    source_sha256 text NOT NULL,
    line_number   integer NOT NULL,
    parse_status  text NOT NULL,
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_file, line_number)
);
CREATE INDEX IF NOT EXISTS service_request_line_stem ON raw.service_request_line (source_stem);

-- Lenient casts: the extracts use '', 'none' and 'NULL' as null markers.
CREATE OR REPLACE FUNCTION core.nn(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN t IS NULL OR btrim(t) = '' OR lower(btrim(t)) IN ('none', 'null') THEN NULL ELSE btrim(t) END
$$;

CREATE OR REPLACE FUNCTION core.try_ts(t text) RETURNS timestamp
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN core.nn(t)::timestamp;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION core.try_num(t text) RETURNS double precision
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN core.nn(t)::double precision;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

CREATE TABLE IF NOT EXISTS core.service_request (
    case_number        text PRIMARY KEY,
    case_number_365    text,
    source_schema      text NOT NULL,          -- 'd365' | 'harvey'
    status             text,
    created_local      timestamp,              -- America/Chicago wall time
    created_utc        timestamptz,
    closed_local       timestamp,
    resolve_by         timestamp,
    case_type          text,
    title              text,
    department         text,
    division           text,
    service_area       text,
    council_district   text,
    key_map            text,
    super_neighborhood text,
    management_district text,
    channel            text,
    incident_address   text,
    incident_street    text,
    incident_city      text,
    incident_state     text,
    zip                text,
    tax_id             text,
    lat_src            double precision,
    lon_src            double precision,
    x_src              double precision,       -- EPSG:2278, US survey feet
    y_src              double precision,
    -- free text: stays in the database, never exported
    description        text,
    latest_notes       text,
    conflict_notes     text,
    resolution_notes   text,
    -- provenance
    extract_ts         timestamptz,
    source_file        text NOT NULL,
    source_sha256      text NOT NULL,
    source_line        integer NOT NULL,
    first_loaded_at    timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    -- Phase 3
    geom               geometry(Point, 4326),
    geocode_method     text,
    geocode_precision  text CHECK (geocode_precision IN ('source_point', 'address', 'intersection', 'street', 'failed')),
    geocode_confidence real,
    geocode_note       text,
    geocoded_at        timestamptz
);
CREATE INDEX IF NOT EXISTS service_request_type ON core.service_request (case_type);
CREATE INDEX IF NOT EXISTS service_request_created ON core.service_request (created_local);
CREATE INDEX IF NOT EXISTS service_request_geom ON core.service_request USING gist (geom);
