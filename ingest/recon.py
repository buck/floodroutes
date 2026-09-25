#!/usr/bin/env python3
"""Phase 0 recon over data/raw/*.txt -> data/recon/<file>.json (cached per file).

Only aggregate counts leave this script. Free text is scanned for keyword
hits but never written out.

Usage: recon.py [--force]      then: recon_report.py
"""
import argparse
import collections
import datetime as dt
import json
import math
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from pyproj import Transformer

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "recon"

BBOX = (-95.95, 29.45, -94.90, 30.20)  # lon_min, lat_min, lon_max, lat_max

# canonical name -> header spellings seen across schemas (compared uppercased, stripped)
CANON = {
    "case_number": ["CASE NUMBER"],
    "case_365": ["365 CASE NUMBER"],
    "address": ["INCIDENT ADDRESS", "SR LOCATION"],
    "lat": ["LATITUDE"],
    "lon": ["LONGITUDE"],
    "x": ["X"],
    "y": ["Y"],
    "created": ["CREATED DATE LOCAL", "SR CREATE DATE"],
    "case_type": ["INCIDENT CASE TYPE", "SR TYPE"],
    "title": ["TITLE"],
    "department": ["DEPARTMENT"],
    "division": ["DIVISION"],
    "taxid": ["TAXID", "TAX ID"],
    "zip": ["ZIP CODE"],
    "channel": ["CHANNEL", "CHANNEL TYPE"],
    "description": ["DESCRIPTION"],
    "latest_notes": ["LATEST CASE NOTES"],
    "conflict_notes": ["SAMPLE CASE CONFLICTS NOTES"],
    "resolution_notes": ["RESOLUTION NOTES"],
}
TEXT_FIELDS = ["title", "description", "latest_notes", "conflict_notes", "resolution_notes"]

FLOOD_TYPE_RE = re.compile(r"flood|drain|ditch|culvert|storm|water|sewer|inlet|pond|"
                           r"street hazard|high water|manhole|underpass|erosion|swale", re.I)
KEYWORDS = {
    "strong": [r"flood", r"high water", r"water in (the )?street", r"street is under ?water",
               r"underpass", r"impassable", r"cars? (were |are )?stalled",
               r"(won'?t|not|doesn'?t|does not) drain\w*.{0,40}\b(street|road)|\b(street|road).{0,40}(won'?t|not|doesn'?t|does not) drain"],
    "weak": [r"standing water", r"ponding", r"clogged (storm )?(inlet|drain)", r"storm ?drain",
             r"\bditch", r"culvert"],
}
KW_RE = {tier: [re.compile(p, re.I) for p in pats] for tier, pats in KEYWORDS.items()}

# likely-included flood/drainage types, for per-type geocode estimates
FLOOD_CORE_RE = re.compile(r"^(Drainage|Flooding|Poor Drainage|Low Area or Standing Water ROW|Street Hazard|"
                           r".*Ditch.*|Inlet Clogged.*|Clean Storm Sewer|Storm Sewer (Repair|Inspection)|"
                           r"Street Drainage Restoration|Flush Roadside Ditch or Culvert|Install Culvert)$", re.I)


def addr_shape(a: str) -> str:
    a = a.strip().upper()
    if not a or a == "NONE":
        return "blank"
    if a.startswith("INTERSECTION") or "&" in a:
        return "intersection_marked"
    if re.match(r"^\d+\s", a):
        return "house_number"
    if re.match(r"^[A-Z0-9 .'-]+ (ST|DR|RD|BLVD|LN|AVE|PKWY|FWY|WAY|CT|CIR|HWY|TRL|PL)\b.*\b(ST|DR|RD|BLVD|LN|AVE|PKWY|FWY|WAY|CT|CIR|HWY|TRL|PL)\b", a):
        return "two_streets_unmarked"
    return "other"


REC_START = re.compile(rb"^\s*\d[\d-]*\s*\|")
HEADER_RE = re.compile(rb"case number", re.I)


def latest_files() -> list[Path]:
    """Newest downloaded copy per source stem."""
    by_stem = {}
    for p in sorted(RAW.glob("*.txt")):
        stem = p.stem.rsplit("_", 1)[0]
        by_stem[stem] = p
    return list(by_stem.values())


def decode(b: bytes, stats) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        stats["non_utf8_lines"] += 1
        return b.decode("cp1252", errors="replace")


def fnum(s):
    if s is None:
        return None
    s = s.strip()
    if not s or s.lower() in ("none", "null", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def haversine_m(lon1, lat1, lon2, lat2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def scan(path: Path) -> dict:
    tf = Transformer.from_crs("EPSG:2278", "EPSG:4326", always_xy=True)
    st = collections.Counter()
    per_year = collections.defaultdict(collections.Counter)
    case_types = collections.Counter()
    title_prefix = collections.Counter()
    depts = collections.Counter()
    divs = collections.Counter()
    type_dept = collections.Counter()
    none_marker = collections.Counter()
    channels = collections.Counter()
    kw_hits = collections.Counter()
    kw_by_type = collections.Counter()
    flood_daily = collections.Counter()
    case_shapes = collections.Counter()
    xy_dist = []
    pts = collections.Counter()
    pts_flood = collections.Counter()
    disagree_ll = collections.Counter()
    disagree_xy = collections.Counter()
    geo_addr = collections.Counter()
    created_range = [None, None]
    case_numbers = []
    banner = []
    header = None
    idx = {}

    raw = path.read_bytes()
    lines = raw.split(b"\n")
    st["physical_lines"] = len(lines)

    # header: first line with "case number" and many pipes
    hdr_line = None
    for i, ln in enumerate(lines[:50]):
        if HEADER_RE.search(ln) and ln.count(b"|") >= 10:
            hdr_line = i
            break
        banner.append(ln.rstrip(b"\r").decode("utf-8", "replace")[:120])
    if hdr_line is None:
        return {"file": path.name, "error": "no header found"}
    header = [h.strip() for h in decode(lines[hdr_line].rstrip(b"\r"), st).split("|")]
    nh = len(header)
    up = [h.upper() for h in header]
    for canon, names in CANON.items():
        for n in names:
            if n in up:
                idx[canon] = up.index(n)
                break

    def get(fields, key):
        i = idx.get(key)
        return fields[i].strip() if i is not None and i < len(fields) else None

    # assemble records: a record starts on a line beginning with a case number
    records = []
    cur = None
    for ln in lines[hdr_line + 1:]:
        ln = ln.rstrip(b"\r")
        if not ln.strip():
            continue
        if re.fullmatch(rb"[-|\s]+", ln):  # separator row under a padded header
            st["separator_lines"] += 1
            continue
        if REC_START.match(ln):
            if cur is not None:
                records.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
            st["continuation_lines"] += 1
        else:
            st["orphan_lines"] += 1
    if cur is not None:
        records.append(cur)

    for rec in records:
        text = "\n".join(decode(b, st) for b in rec)
        fields = text.split("|")
        st["records"] += 1
        nf = len(fields)
        if len(rec) > 1:
            st["records_multiline"] += 1
        if nf != nh:
            st["malformed"] += 1
            st["malformed_too_many" if nf > nh else "malformed_too_few"] += 1
            if nf < nh:
                continue  # can't trust column positions
            # extra pipes: assume they sit in the trailing free-text columns
        for i, v in enumerate(fields[:nh]):
            if v.strip().lower() == "none":
                none_marker[header[i]] += 1

        cn = get(fields, "case_number") or ""
        case_numbers.append(cn)
        case_shapes[re.sub(r"\d", "9", cn)[:30]] += 1
        created = get(fields, "created") or ""
        year = created[:4] if re.match(r"\d{4}", created) else "unknown"
        y = per_year[year]
        y["records"] += 1
        if created[:10]:
            created_range[0] = min(created_range[0] or created[:10], created[:10])
            created_range[1] = max(created_range[1] or created[:10], created[:10])
        ctype0 = get(fields, "case_type") or ""
        is_core = bool(FLOOD_CORE_RE.match(ctype0))
        if is_core:
            y["flood_core"] += 1
        desc = get(fields, "description") or ""
        if desc and desc.lower() != "none":
            y["desc_populated"] += 1
        notes = " ".join(v for v in (get(fields, f) or "" for f in ("latest_notes", "conflict_notes", "resolution_notes"))
                         if v and v.lower() != "none")
        if notes:
            y["notes_populated"] += 1

        lat, lon = fnum(get(fields, "lat")), fnum(get(fields, "lon"))
        if lat is not None and lon is not None:
            y["latlon_populated"] += 1
            if lat == 0 or lon == 0:
                y["latlon_zero"] += 1
            elif BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]:
                y["latlon_in_bbox"] += 1
        good_ll = lat and lon and BBOX[0] <= lon <= BBOX[2] and BBOX[1] <= lat <= BBOX[3]

        x, yy = fnum(get(fields, "x")), fnum(get(fields, "y"))
        good_xy = False
        if x is not None and yy is not None:
            y["xy_populated"] += 1
            if x == 0 or yy == 0:
                y["xy_zero"] += 1
            else:
                xl, yl = tf.transform(x, yy)
                if BBOX[0] <= xl <= BBOX[2] and BBOX[1] <= yl <= BBOX[3]:
                    y["xy_in_bbox"] += 1
                    good_xy = True
                    if good_ll:
                        d = haversine_m(lon, lat, xl, yl)
                        if len(xy_dist) < 20000:
                            xy_dist.append(round(d, 1))
                        if d > 100:
                            y["latlon_xy_disagree_100m"] += 1
                            disagree_ll[f"{lon:.4f},{lat:.4f}"] += 1
                            disagree_xy[f"{xl:.4f},{yl:.4f}"] += 1
        if good_ll:
            pts[f"{lon:.4f},{lat:.4f}"] += 1
            if is_core:
                pts_flood[f"{lon:.4f},{lat:.4f}"] += 1
        if good_ll or good_xy:
            y["has_point"] += 1
        else:
            y["needs_geocode"] += 1
            shape = addr_shape(get(fields, "address") or "")
            geo_addr[shape] += 1
            if shape == "intersection_marked":
                y["needs_geocode_intersection"] += 1
            if is_core:
                y["flood_core_needs_geocode"] += 1
                geo_addr["flood_core:" + shape] += 1
            if get(fields, "taxid") and get(fields, "taxid").lower() != "none":
                y["needs_geocode_has_taxid"] += 1
        if get(fields, "taxid"):
            y["taxid_populated"] += 1

        ctype = get(fields, "case_type") or ""
        title = get(fields, "title") or ""
        tprefix = re.split(r"\s*-\s*\d", title, maxsplit=1)[0].strip()
        dept, div = get(fields, "department") or "", get(fields, "division") or ""
        case_types[ctype] += 1
        title_prefix[tprefix] += 1
        depts[dept] += 1
        divs[div] += 1
        type_dept[f"{ctype}\t{dept}\t{div}"] += 1
        channels[get(fields, "channel") or ""] += 1

        body = " ".join(get(fields, f) or "" for f in TEXT_FIELDS)
        body_nt = " ".join(get(fields, f) or "" for f in TEXT_FIELDS if f != "title")
        for tier in ("strong", "weak"):
            if any(rx.search(body_nt) for rx in KW_RE[tier]):
                y[f"kwnt_{tier}"] += 1
                if not is_core:
                    y[f"kwnt_{tier}_other_types"] += 1
                break
        tier_hit = None
        for tier in ("strong", "weak"):
            for rx in KW_RE[tier]:
                if rx.search(body):
                    kw_hits[f"{tier}:{rx.pattern}"] += 1
                    tier_hit = tier_hit or tier
        if tier_hit:
            y[f"kw_{tier_hit}"] += 1
            kw_by_type[f"{tier_hit}\t{ctype}"] += 1
        if FLOOD_TYPE_RE.search(ctype) and created[:10]:
            flood_daily[f"{ctype}\t{created[:10]}"] += 1

    xy_dist.sort()
    q = lambda p: xy_dist[min(len(xy_dist) - 1, int(p * len(xy_dist)))] if xy_dist else None
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "banner": banner,
        "header": header,
        "canon_index": idx,
        "stats": dict(st),
        "per_year": {k: dict(v) for k, v in per_year.items()},
        "case_types": dict(case_types),
        "title_prefix": dict(title_prefix.most_common(400)),
        "departments": dict(depts),
        "divisions": dict(divs),
        "type_dept_div": dict(type_dept),
        "none_marker": dict(none_marker),
        "channels": dict(channels),
        "kw_hits": dict(kw_hits),
        "kw_by_type": dict(kw_by_type),
        "flood_type_daily": dict(flood_daily),
        "case_number_shapes": dict(case_shapes.most_common(15)),
        "created_range": created_range,
        "top_points": dict(pts.most_common(15)),
        "top_points_flood_core": dict(pts_flood.most_common(15)),
        "disagree_top_latlon": dict(disagree_ll.most_common(10)),
        "disagree_top_xy": dict(disagree_xy.most_common(10)),
        "needs_geocode_address_shapes": dict(geo_addr),
        "latlon_vs_xy_m": {"n": len(xy_dist), "p50": q(.5), "p90": q(.9), "p99": q(.99), "max": q(1.0)},
        "_cases": case_numbers,
        "scanned_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def run(path: Path) -> str:
    out = OUT / (path.name + ".json")
    res = scan(path)
    cases = res.pop("_cases", None)
    out.write_text(json.dumps(res))
    if cases is not None:
        (OUT / (path.name + ".cases")).write_text("\n".join(cases))
    s = res.get("stats", {})
    return f"{path.name}: {s.get('records', 0):,} records, {s.get('malformed', 0):,} malformed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    todo = [p for p in latest_files()
            if args.force or not (OUT / (p.name + ".json")).exists()]
    print(f"{len(todo)} files to scan", flush=True)
    with ProcessPoolExecutor(args.workers) as ex:
        for msg in ex.map(run, todo):
            print(msg, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
