#!/usr/bin/env python3
"""Phase 3: give every core.service_request a geom, geocode_method and geocode_precision.

Queue-driven, batched and resumable. Flood-signal cases go first. Reruns skip
anything already geocoded (geocoded_at IS NOT NULL) unless --redo.

  1. Source points (SQL, batches of --batch):
       lat/lon inside the Houston bbox, nonzero, not a placeholder point;
       X/Y (EPSG:2278) transformed to 4326 and inside the bbox.
       Both valid and > 100 m apart -> trust X/Y, note 'latlon_xy_disagree'.
  2. US Census batch geocoder for what is left and has a house-number address
     (<= 10,000 per request; checkpoint after each batch).
  3. Everything else -> precision 'failed' with a reason in geocode_note.

Intersections and HCAD matching are not attempted: recon found no
intersection-style addresses among cases lacking a source point outside the
Harvey file, and HCAD has no parcel geometry (see docs/recon.md).

Usage: geocode.py [--batch 5000] [--priority 0] [--skip-census] [--redo] [--report-only]
  --priority 0   flood-signal cases only (the rest can run later in the background)
"""
import argparse
import csv
import io
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from db import connect  # noqa: E402

BBOX = (-95.95, 29.45, -94.90, 30.20)
DISAGREE_M = 100
# Points shared by >= 2000 records across >= 40 case types are office placeholders
# (City Hall and two other downtown government addresses). Recomputed each run.
PLACEHOLDER_MIN_N, PLACEHOLDER_MIN_TYPES = 2000, 40
CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CENSUS_BATCH = 5000

DDL = """
CREATE TABLE IF NOT EXISTS core.geocode_queue (
    case_number text PRIMARY KEY REFERENCES core.service_request,
    priority    smallint NOT NULL,          -- 0 = flood signal, 1 = everything else
    status      text NOT NULL DEFAULT 'pending',  -- pending | done | census_pending | failed
    attempts    smallint NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS geocode_queue_todo ON core.geocode_queue (status, priority, case_number);
CREATE TABLE IF NOT EXISTS core.geocode_placeholder (
    lon numeric, lat numeric, records integer, case_types integer,
    PRIMARY KEY (lon, lat)
);
"""

SOURCE_UPDATE = f"""
WITH b AS (
    SELECT q.case_number FROM core.geocode_queue q
    WHERE q.status = 'pending' AND q.priority = %(prio)s
    ORDER BY q.case_number LIMIT %(n)s
    FOR UPDATE SKIP LOCKED
), p AS (
    SELECT r.case_number,
        CASE WHEN r.lat_src BETWEEN {BBOX[1]} AND {BBOX[3]} AND r.lon_src BETWEEN {BBOX[0]} AND {BBOX[2]}
              AND NOT EXISTS (SELECT 1 FROM core.geocode_placeholder ph
                              WHERE ph.lon = round(r.lon_src::numeric, 4) AND ph.lat = round(r.lat_src::numeric, 4))
             THEN ST_SetSRID(ST_MakePoint(r.lon_src, r.lat_src), 4326) END AS ll,
        CASE WHEN r.x_src > 0 AND r.y_src > 0
             THEN ST_Transform(ST_SetSRID(ST_MakePoint(r.x_src, r.y_src), 2278), 4326) END AS xy0
    FROM core.service_request r JOIN b USING (case_number)
), q AS (
    SELECT case_number, ll,
        CASE WHEN ST_X(xy0) BETWEEN {BBOX[0]} AND {BBOX[2]} AND ST_Y(xy0) BETWEEN {BBOX[1]} AND {BBOX[3]}
              AND NOT EXISTS (SELECT 1 FROM core.geocode_placeholder ph
                              WHERE ph.lon = round(ST_X(xy0)::numeric, 4) AND ph.lat = round(ST_Y(xy0)::numeric, 4))
             THEN xy0 END AS xy
    FROM p
), g AS (
    SELECT case_number,
        CASE WHEN xy IS NOT NULL AND (ll IS NULL OR ST_DistanceSphere(ll, xy) > {DISAGREE_M}) THEN xy ELSE ll END AS geom,
        CASE WHEN ll IS NOT NULL AND xy IS NOT NULL AND ST_DistanceSphere(ll, xy) > {DISAGREE_M} THEN 'source_xy'
             WHEN ll IS NOT NULL THEN 'source_latlon'
             WHEN xy IS NOT NULL THEN 'source_xy' END AS method,
        CASE WHEN ll IS NOT NULL AND xy IS NOT NULL AND ST_DistanceSphere(ll, xy) > {DISAGREE_M}
             THEN 'latlon_xy_disagree_' || round(ST_DistanceSphere(ll, xy))::int || 'm' END AS note
    FROM q
), u AS (
    UPDATE core.service_request r SET
        geom = g.geom, geocode_method = g.method,
        geocode_precision = CASE WHEN g.geom IS NOT NULL THEN 'source_point' END,
        geocode_confidence = CASE WHEN g.geom IS NOT NULL THEN 0.95 END,
        geocode_note = g.note,
        geocoded_at = CASE WHEN g.geom IS NOT NULL THEN now() END
    FROM g WHERE r.case_number = g.case_number
    RETURNING r.case_number, g.geom IS NOT NULL AS ok
)
UPDATE core.geocode_queue q SET
    status = CASE WHEN u.ok THEN 'done' ELSE 'census_pending' END,
    attempts = q.attempts + 1, updated_at = now()
FROM u WHERE q.case_number = u.case_number
"""


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def fill_queue(conn, redo):
    conn.execute(DDL)
    if redo:
        conn.execute("TRUNCATE core.geocode_queue")
        conn.execute("""UPDATE core.service_request SET geom = NULL, geocode_method = NULL,
                        geocode_precision = NULL, geocode_confidence = NULL, geocode_note = NULL, geocoded_at = NULL
                        WHERE geocoded_at IS NOT NULL OR geocode_precision IS NOT NULL""")
    n = conn.execute("""
        INSERT INTO core.geocode_queue (case_number, priority)
        SELECT r.case_number, CASE WHEN f.case_number IS NULL THEN 1 ELSE 0 END
        FROM core.service_request r LEFT JOIN core.flood_signal f USING (case_number)
        WHERE r.geocoded_at IS NULL AND r.geocode_precision IS NULL
        ON CONFLICT (case_number) DO NOTHING""").rowcount
    # a case that became a flood signal after it was queued moves up
    conn.execute("""UPDATE core.geocode_queue q SET priority = 0 FROM core.flood_signal f
                    WHERE q.case_number = f.case_number AND q.priority <> 0""")
    conn.execute("TRUNCATE core.geocode_placeholder")
    conn.execute(f"""
        INSERT INTO core.geocode_placeholder
        SELECT round(lon_src::numeric, 4), round(lat_src::numeric, 4), count(*), count(DISTINCT case_type)
        FROM core.service_request
        WHERE lat_src BETWEEN {BBOX[1]} AND {BBOX[3]} AND lon_src BETWEEN {BBOX[0]} AND {BBOX[2]}
        GROUP BY 1, 2 HAVING count(*) >= %s AND count(DISTINCT case_type) >= %s""",
                 (PLACEHOLDER_MIN_N, PLACEHOLDER_MIN_TYPES))
    ph = conn.execute("SELECT lon, lat, records FROM core.geocode_placeholder ORDER BY records DESC").fetchall()
    log(f"queued {n:,} new cases; placeholders: " + ", ".join(f"({lo},{la}) n={r:,}" for lo, la, r in ph))


def source_points(conn, batch, prios):
    for prio in prios:
        todo = conn.execute("SELECT count(*) FROM core.geocode_queue WHERE status='pending' AND priority=%s",
                            (prio,)).fetchone()[0]
        done, t0 = 0, time.time()
        while True:
            n = conn.execute(SOURCE_UPDATE, {"prio": prio, "n": batch}).rowcount
            conn.commit()  # checkpoint
            if n == 0:
                break
            done += n
            if done % (batch * 20) < batch or done >= todo:
                rate = done / max(time.time() - t0, 1e-6)
                log(f"source points prio={prio}: {done:,}/{todo:,} "
                    f"({rate:,.0f}/s, ETA {(todo - done) / max(rate, 1e-6) / 60:.1f} min)")


def split_address(a: str):
    """'18315 TOMBALL PKY HOUSTON TX 77070' -> (street, city, state, zip)."""
    import re
    a = re.sub(r"\s+", " ", a.replace(",", " ")).strip()
    m = re.match(r"^(.*?)\s+([A-Z][A-Z .]+?)\s+(TX|TEXAS)\s*(\d{5})?(-\d{4})?$", a, re.I)
    if m:
        return m.group(1), m.group(2), "TX", m.group(4) or ""
    return a, "Houston", "TX", ""


def census(conn, prios):
    rows = conn.execute(r"""
        SELECT q.case_number, r.incident_address, r.zip
        FROM core.geocode_queue q JOIN core.service_request r USING (case_number)
        WHERE q.status = 'census_pending' AND q.priority = ANY(%s)
          AND r.incident_address ~ '^\s*\d+[A-Z]?\s+\S'
        ORDER BY q.priority, q.case_number""", (prios,)).fetchall()
    log(f"census candidates (house-number addresses): {len(rows):,}")
    client = httpx.Client(timeout=600)
    for i in range(0, len(rows), CENSUS_BATCH):
        chunk = rows[i:i + CENSUS_BATCH]
        buf = io.StringIO()
        w = csv.writer(buf)
        for cn, addr, zp in chunk:
            street, city, state, z = split_address(addr)
            w.writerow([cn, street, city, state, z or (zp or "")])
        for attempt in range(1, 6):
            try:
                resp = client.post(CENSUS_URL, data={"benchmark": "Public_AR_Current"},
                                   files={"addressFile": ("batch.csv", buf.getvalue(), "text/csv")})
                resp.raise_for_status()
                break
            except httpx.HTTPError as e:
                log(f"census batch {i // CENSUS_BATCH + 1} attempt {attempt} failed: {type(e).__name__}")
                time.sleep(30 * attempt)
        else:
            log("census unavailable; leaving these as census_pending for the next run")
            return
        hits = 0
        with conn.transaction():
            for rec in csv.reader(io.StringIO(resp.text)):
                if len(rec) < 3:
                    continue
                cn, status = rec[0], rec[2]
                if status == "Match" and len(rec) >= 6 and "," in rec[5]:
                    lon, lat = map(float, rec[5].split(","))
                    exact = rec[3] == "Exact"
                    conn.execute("""UPDATE core.service_request SET geom = ST_SetSRID(ST_MakePoint(%s,%s),4326),
                                    geocode_method='census_batch', geocode_precision='address',
                                    geocode_confidence=%s, geocode_note=%s, geocoded_at=now()
                                    WHERE case_number=%s""",
                                 (lon, lat, 0.9 if exact else 0.7, "census_" + rec[3].lower(), cn))
                    conn.execute("UPDATE core.geocode_queue SET status='done', attempts=attempts+1, updated_at=now() WHERE case_number=%s", (cn,))
                    hits += 1
                else:
                    conn.execute("UPDATE core.geocode_queue SET status='failed', attempts=attempts+1, updated_at=now() WHERE case_number=%s", (cn,))
                    conn.execute("""UPDATE core.service_request SET geocode_precision='failed', geocode_method='census_batch',
                                    geocode_note=%s, geocoded_at=now() WHERE case_number=%s""",
                                 ("census_" + status.lower().replace(" ", "_"), cn))
        log(f"census batch {i // CENSUS_BATCH + 1}: {hits}/{len(chunk)} matched (checkpointed)")
        time.sleep(2)


def mark_failed(conn, prios):
    n = conn.execute("""
        WITH f AS (
            UPDATE core.geocode_queue q SET status='failed', attempts=attempts+1, updated_at=now()
            FROM core.service_request r
            WHERE q.case_number = r.case_number AND q.status = 'census_pending' AND q.priority = ANY(%s)
              AND (r.incident_address IS NULL OR r.incident_address !~ '^\\s*\\d+[A-Z]?\\s+\\S')
            RETURNING q.case_number, r.incident_address IS NULL AS blank)
        UPDATE core.service_request r SET geocode_precision='failed', geocode_method='none',
            geocode_note = CASE WHEN f.blank THEN 'no_source_point_no_address' ELSE 'no_source_point_unparseable_address' END,
            geocoded_at = now()
        FROM f WHERE r.case_number = f.case_number""", (prios,)).rowcount
    conn.commit()
    log(f"marked {n:,} failed (no usable point or address)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--priority", type=int, choices=[0, 1], help="only this queue priority")
    ap.add_argument("--skip-census", action="store_true")
    ap.add_argument("--redo", action="store_true", help="clear all geocodes and start over")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--no-report", action="store_true", help="skip docs/geocode_report.md (nightly runs)")
    args = ap.parse_args()
    with connect() as conn:
        if not args.report_only:
            fill_queue(conn, args.redo)
            conn.commit()
            prios = [args.priority] if args.priority is not None else [0, 1]
            source_points(conn, args.batch, prios)
            if not args.skip_census:
                census(conn, prios)
            mark_failed(conn, prios)
            conn.execute("ANALYZE core.service_request")
    if not args.no_report:
        import report
        report.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
