#!/usr/bin/env python3
"""Load downloaded 311 extracts into raw.service_request_line and upsert core.service_request.

Each file is loaded in one transaction: replace the stem's previous raw
snapshot, COPY the new rows, upsert core, and record it in raw.load_log. A
killed run rolls back that file, and a rerun skips files already in load_log.

Core upserts only replace a case when the incoming extract is at least as new
as the stored one, so file order doesn't matter and reruns change nothing.
Geocode columns are never touched here.

Usage: load.py [--only REGEX] [--force]
"""
import argparse
import csv
import datetime as dt
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse311  # noqa: E402
from db import connect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"
CHICAGO = ZoneInfo("America/Chicago")

# core column <- SQL expression over raw columns (union of D365 and Harvey names)
CORE_MAP = [
    ("case_number",         "core.nn(r.case_number)"),
    ("case_number_365",     "core.nn(r.case_number_365)"),
    ("source_schema",       "CASE WHEN r.sr_type IS NOT NULL THEN 'harvey' ELSE 'd365' END"),
    ("status",              "core.nn(r.status)"),
    ("created_local",       "core.try_ts(coalesce(r.created_date_local, r.sr_create_date))"),
    ("created_utc",         "coalesce(core.try_ts(r.created_date_utc) AT TIME ZONE 'UTC', "
                            "core.try_ts(r.sr_create_date) AT TIME ZONE 'America/Chicago')"),
    ("closed_local",        "core.try_ts(coalesce(r.closed_date, r.date_closed))"),
    ("resolve_by",          "core.try_ts(coalesce(r.resolve_by_time, r.due_date))"),
    ("case_type",           "core.nn(coalesce(r.incident_case_type, r.sr_type))"),
    ("title",               "core.nn(r.title)"),
    ("department",          "core.nn(r.department)"),
    ("division",            "core.nn(r.division)"),
    ("service_area",        "core.nn(r.service_area)"),
    ("council_district",    "core.nn(coalesce(r.council_district, r.district))"),
    ("key_map",             "core.nn(r.key_map)"),
    ("super_neighborhood",  "core.nn(coalesce(r.customer_superneighborhood, r.neighborhood))"),
    ("management_district", "core.nn(r.management_district)"),
    ("channel",             "core.nn(coalesce(r.channel, r.channel_type))"),
    ("incident_address",    "core.nn(coalesce(r.incident_address, r.sr_location))"),
    ("incident_street",     "core.nn(r.incident_street)"),
    ("incident_city",       "core.nn(r.incident_city)"),
    ("incident_state",      "core.nn(r.incident_state)"),
    ("zip",                 "core.nn(r.zip_code)"),
    ("tax_id",              "core.nn(coalesce(r.taxid, r.tax_id))"),
    ("lat_src",             "core.try_num(r.latitude)"),
    ("lon_src",             "core.try_num(r.longitude)"),
    ("x_src",               "core.try_num(r.x)"),
    ("y_src",               "core.try_num(r.y)"),
    ("description",         "core.nn(r.description)"),
    ("latest_notes",        "core.nn(r.latest_case_notes)"),
    ("conflict_notes",      "core.nn(r.sample_case_conflicts_notes)"),
    ("resolution_notes",    "core.nn(r.resolution_notes)"),
    ("extract_ts",          "coalesce(core.try_ts(r.extract_date) AT TIME ZONE 'UTC', l.extract_ts)"),
    ("source_file",         "r.source_file"),
    ("source_sha256",       "r.source_sha256"),
    ("source_line",         "r.line_number"),
]
CORE_COLS = [c for c, _ in CORE_MAP]
CORE_SELECT = ",\n    ".join(f"{e} AS {c}" for c, e in CORE_MAP)

# every raw column CORE_SELECT refers to must exist even if no file has it yet
REFERENCED_RAW = sorted(set(re.findall(r"\br\.([a-z0-9_]+)", CORE_SELECT)) -
                        {"source_file", "source_sha256", "line_number"})


def latest_snapshots() -> list[dict]:
    rows = list(csv.DictReader((RAW / "manifest.tsv").open(), delimiter="\t"))
    latest = {}
    for r in rows:
        if (RAW / r["file"]).exists():
            latest[r["url"]] = r  # manifest is append-only, so later rows win
    out = list(latest.values())

    def order(r):  # Harvey first, then years ascending, MTD last
        f = r["file"]
        if "Harvey" in f:
            return (0, f)
        m = re.search(r"compressed-(\d{4})", f)
        return (1, m.group(1)) if m else (2, f)
    return sorted(out, key=order)


def stem_of(fname: str) -> str:
    return fname.rsplit("_", 1)[0]


def ensure_columns(cur, cols):
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema='raw' AND table_name='service_request_line'""")
    have = {r[0] for r in cur.fetchall()}
    for c in cols:
        if c not in have:
            cur.execute(f'ALTER TABLE raw.service_request_line ADD COLUMN IF NOT EXISTS "{c}" text')


def extract_ts(meta: parse311.Parsed, man: dict):
    if meta.extract_banner:
        return dt.datetime.fromisoformat(meta.extract_banner).replace(tzinfo=CHICAGO)
    # no banner (Harvey-clean): fall back to the server's Last-Modified
    return dt.datetime.strptime(man["last_modified"], "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=dt.timezone.utc)


def load_file(conn, man: dict, force: bool) -> str:
    fname, sha = man["file"], man["sha256"]
    stem = stem_of(fname)
    with conn.cursor() as cur:
        cur.execute("SELECT core_upserted_at FROM raw.load_log WHERE source_file=%s AND source_sha256=%s",
                    (fname, sha))
        r = cur.fetchone()
        if r and r[0] and not force:
            return f"skip  {fname} (loaded)"

    t0 = time.time()
    meta, records = parse311.read(RAW / fname)
    cols = meta.columns
    counts = {"ok": 0, "extra_fields_joined": 0, "missing_last_field": 0, "short": 0}
    with conn.transaction(), conn.cursor() as cur:
        ensure_columns(cur, cols + REFERENCED_RAW)
        # older snapshots of this stem, and this file itself if forced
        cur.execute("DELETE FROM raw.load_log WHERE source_stem=%s", (stem,))
        cur.execute("""INSERT INTO raw.load_log (source_file, source_stem, source_sha256, extract_ts, header)
                       VALUES (%s,%s,%s,%s,%s)""", (fname, stem, sha, extract_ts(meta, man), meta.header))
        collist = ", ".join(f'"{c}"' for c in cols)
        with cur.copy(f"""COPY raw.service_request_line
                          (source_file, source_stem, source_sha256, line_number, parse_status, {collist})
                          FROM STDIN""") as cp:
            for line_no, fields, status in records:
                counts[status] += 1
                cp.write_row([fname, stem, sha, line_no, status, *fields])
        n = sum(counts.values())
        cur.execute(f"""
            INSERT INTO core.service_request ({", ".join(CORE_COLS)})
            SELECT DISTINCT ON (case_number) * FROM (
                SELECT {CORE_SELECT}
                FROM raw.service_request_line r JOIN raw.load_log l USING (source_file)
                WHERE r.source_file = %s AND r.parse_status <> 'short'
            ) s
            WHERE case_number IS NOT NULL
            ORDER BY case_number, source_line DESC
            ON CONFLICT (case_number) DO UPDATE SET
                {", ".join(f"{c} = excluded.{c}" for c in CORE_COLS if c != "case_number")},
                updated_at = now()
            WHERE excluded.extract_ts >= core.service_request.extract_ts
        """, (fname,))
        upserted = cur.rowcount
        cur.execute("""UPDATE raw.load_log SET records=%s, parse_ok=%s, parse_extra=%s, parse_short=%s,
                              core_upserted=%s, core_upserted_at=now()
                       WHERE source_file=%s""",
                    (n, counts["ok"] + counts["missing_last_field"], counts["extra_fields_joined"], counts["short"], upserted, fname))
    return (f"load  {fname}: {n:,} rows (extra-joined {counts['extra_fields_joined']}, "
            f"missing-last {counts['missing_last_field']}, short {counts['short']}), core upserted {upserted:,} in {time.time() - t0:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="regex on file name")
    ap.add_argument("--force", action="store_true", help="reload even if already loaded")
    args = ap.parse_args()
    with connect(autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
        for man in latest_snapshots():
            if args.only and not re.search(args.only, man["file"]):
                continue
            print(load_file(conn, man, args.force), flush=True)
        conn.execute("ANALYZE core.service_request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
