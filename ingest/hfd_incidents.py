#!/usr/bin/env python3
"""Load HFD water-rescue and flood incidents (2019 - mid 2025) into core.hfd_flood_incident.

Source: 2.46M geocoded Houston Fire Department incidents published by a Rice University
GIS account (dlr10_ricegis) as the ArcGIS layer HFD_FinalSituation_XYTableToPoint, part of
a Rice study of HFD false alarms and station workload. Not an official HFD publication:
treat as research data and do not republish without asking Rice / HFD.

Selection (1,570 incidents when written):
  - dispatched as a water rescue: CAD code FFWR*  (FFWRAA, FFWRBB, FFWRBA, FFWRB, FFWRA, ...)
  - or final NFIRS code 363 swift water rescue, 521 water evacuation, 812 flood assessment
Left out on purpose: FFSCFL (mostly plumbing leaks), FFLH* (lake/harbor rescues), FEDR*
(drowning EMS calls, e.g. pools).

signal:
  rescue              people rescued or evacuated from water (NFIRS 342, 360, 361, 363, 365, 521)
  assessment          flood assessment / severe weather (812, 800)
  reported_not_found  water-rescue dispatch that found nothing or was cancelled (622, 611):
                      someone still reported people in water at that spot
  other               water-rescue dispatch closed as something else (crash, EMS, public assist)

Each incident is linked to core.storm_day / core.storm_event by date, like the 311 signals.
Source quirks: incidentId is the placeholder 0 on ~720 rows (so rows are keyed on OBJECTID), and
~300 rows from 2024-25 have no date at all, only year/month/hour (date_precision = 'month'; most
are July 2024, i.e. Hurricane Beryl).
NOT USED for now: nothing reads this table (risk/build_risk.py, the map and the API ignore it) until
Rice / HFD agree to its use. See README "HFD water-rescue incidents (loaded, not used)".

Usage: .venv/bin/python ingest/hfd_incidents.py
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import connect  # noqa: E402

URL = ("https://services.arcgis.com/lqRTrQp2HrfnJt8U/arcgis/rest/services/"
       "HFD_FinalSituation_XYTableToPoint/FeatureServer/0/query")
WHERE = "cadCodeFinal LIKE 'FFWR%' OR nfirsCodeFinal IN (363,521,812)"
SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "external" / "hfd_flood_incidents_rice.json"

RESCUE = {342, 360, 361, 363, 365, 521}
ASSESS = {800, 812}
NOT_FOUND = {611, 622}

DDL = """
DROP TABLE IF EXISTS core.hfd_flood_incident;
CREATE TABLE core.hfd_flood_incident (
    source_objectid    integer PRIMARY KEY,  -- OBJECTID in the Rice layer (incidentId is often 0)
    incident_id        bigint,               -- NULL where the source has the placeholder 0
    alternate_id       bigint,
    occurred_at        timestamp,            -- local time; dispatch time when occurred is missing
    dispatched_at      timestamp,
    local_day          date,                 -- NULL for ~300 2024-25 rows that carry only year/month/hour
    year               integer,
    month              integer,
    hour               integer,
    date_precision     text NOT NULL CHECK (date_precision IN ('day', 'month')),
    signal             text NOT NULL CHECK (signal IN ('rescue', 'assessment', 'reported_not_found', 'other')),
    cad_code           text,
    nfirs_code         integer,
    nfirs_desc         text,
    nfirs_group        text,
    priority           text,
    cancelled          boolean,
    emergent           boolean,
    station_id         integer,
    first_arriver      text,
    first_arriver_home text,
    zip                text,
    city               text,
    storm_event_id     integer,   -- no FK: classify.py renumbers storm events and re-links this nightly
    geom               geometry(Point, 4326),
    source             text NOT NULL,
    loaded_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hfd_flood_incident_geom ON core.hfd_flood_incident USING gist (geom);
CREATE INDEX IF NOT EXISTS hfd_flood_incident_day ON core.hfd_flood_incident (local_day);
"""


def fetch() -> list[dict]:
    rows, offset = [], 0
    with httpx.Client(timeout=120, headers={"User-Agent": "floodroutes/1.0"}) as c:
        while True:
            js = c.get(URL, params={"where": WHERE, "outFields": "*", "returnGeometry": "false",
                                    "orderByFields": "OBJECTID", "resultOffset": offset,
                                    "resultRecordCount": 1000, "f": "json"}).json()
            if "error" in js:
                raise RuntimeError(js["error"])
            batch = [f["attributes"] for f in js.get("features", [])]
            rows += batch
            if not js.get("exceededTransferLimit") or not batch:
                return rows
            offset += len(batch)
            time.sleep(0.2)


def ts(d, t):
    if not d:
        return None
    h, m, s = ((t or "0:0:0").split(":") + ["0", "0"])[:3]   # times come as "6:27:29" too
    return dt.datetime.fromisoformat(d).replace(hour=int(h), minute=int(m), second=int(float(s)))


def signal(nfirs):
    if nfirs in RESCUE:
        return "rescue"
    if nfirs in ASSESS:
        return "assessment"
    if nfirs in NOT_FOUND:
        return "reported_not_found"
    return "other"


def main():
    rows = fetch()
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps({"fetched": dt.datetime.now().isoformat(timespec="seconds"),
                                    "url": URL, "where": WHERE, "rows": rows}))
    recs = []
    for r in rows:
        occ = ts(r.get("occurredOnLocalDate"), r.get("occurredOnLocalTime"))
        disp = ts(r.get("dispatchedOnLocalDate"), r.get("dispatchedOnLocalTime"))
        when = occ or disp
        lat, lon = r.get("latitude"), r.get("longitude")
        geom = f"SRID=4326;POINT({lon} {lat})" if lat and lon else None
        recs.append((r["OBJECTID"], r["incidentId"] or None, r.get("alternateId") or None, when, disp,
                     when.date() if when else None, r.get("year"), r.get("monthOfYear"), r.get("hourOfDay"),
                     "day" if when else "month", signal(r.get("nfirsCodeFinal")),
                     r.get("cadCodeFinal"), r.get("nfirsCodeFinal"), r.get("nfirsCodeDescriptionFinal"),
                     r.get("nfirsGroupDescriptionFinal"), r.get("responsePriorityCodeFinal"),
                     r.get("cancelled") == "True", r.get("emergent") == "True", r.get("stationId"),
                     r.get("firstArriver"), r.get("firstArriverHomeStation"),
                     str(r["zipCodeLookup"]) if r.get("zipCodeLookup") else None, r.get("cityLookup"),
                     geom, "rice:dlr10_ricegis/HFD_FinalSituation_XYTableToPoint"))

    with connect() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        with cur.copy("""COPY core.hfd_flood_incident (source_objectid, incident_id, alternate_id, occurred_at,
                         dispatched_at, local_day, year, month, hour, date_precision, signal, cad_code, nfirs_code, nfirs_desc, nfirs_group, priority, cancelled,
                         emergent, station_id, first_arriver, first_arriver_home, zip, city, geom, source)
                         FROM STDIN""") as cp:
            for rec in recs:
                cp.write_row(rec)
        cur.execute("""UPDATE core.hfd_flood_incident h SET storm_event_id = d.event_id
                         FROM core.storm_day d WHERE d.day = h.local_day""")
        cur.execute("""SELECT signal, count(*), count(geom), count(local_day), count(storm_event_id)
                         FROM core.hfd_flood_incident GROUP BY 1 ORDER BY 2 DESC""")
        print(f"fetched {len(rows):,} rows, loaded {len(recs):,} incidents")
        print(f"{'signal':20} {'n':>6} {'geom':>6} {'dated':>6} {'storm':>6}")
        for s, n, g, d, e in cur.fetchall():
            print(f"{s:20} {n:>6} {g:>6} {d:>6} {e:>6}")
        cur.execute("""SELECT year, month, count(*) FROM core.hfd_flood_incident WHERE date_precision = 'month'
                        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5""")
        print("month-only rows, busiest months:", ", ".join(f"{y}-{m:02d}: {n}" for y, m, n in cur.fetchall()))
        cur.execute("""SELECT e.label, e.peak_day, count(*) FROM core.hfd_flood_incident h
                         JOIN core.storm_event e ON e.event_id = h.storm_event_id
                        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 8""")
        print("\nbusiest storm events:")
        for label, peak, n in cur.fetchall():
            print(f"  {peak}  {n:>4}  {label or ''}")


if __name__ == "__main__":
    main()
