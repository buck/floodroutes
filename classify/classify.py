#!/usr/bin/env python3
"""Phase 2: classify flood-relevant 311 requests into core.flood_signal.

Rebuilds core.storm_day, core.storm_event and core.flood_signal from scratch in
one transaction, so it is idempotent and safe to rerun after editing
classify/rules.yaml. Writes docs/classification.md (aggregates only) and, with
--sample, docs/classification_sample.csv (redacted text; gitignored).

Usage: classify.py [--sample]
"""
import argparse
import bisect
import collections
import csv
import datetime as dt
import hashlib
import random
import re
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from db import connect  # noqa: E402

RULES = Path(__file__).resolve().parent / "rules.yaml"
DOC = ROOT / "docs" / "classification.md"
SAMPLE = ROOT / "docs" / "classification_sample.csv"
TIERS = ["weak", "moderate", "strong"]
PREFILTER = r"flood|water|drain|ditch|culvert|pond|underpass|impassable|stall|strand|submerg|inlet|sewer"

DDL = """
CREATE TABLE IF NOT EXISTS core.storm_event (
    event_id    integer PRIMARY KEY,
    start_day   date NOT NULL,
    end_day     date NOT NULL,
    peak_day    date NOT NULL,
    peak_count  integer NOT NULL,
    storm_days  integer NOT NULL,
    label       text,
    rules_sha   text NOT NULL
);
CREATE TABLE IF NOT EXISTS core.storm_day (
    day         date PRIMARY KEY,
    drainage_requests integer NOT NULL,
    event_id    integer NOT NULL REFERENCES core.storm_event
);
CREATE TABLE IF NOT EXISTS core.flood_signal (
    case_number  text PRIMARY KEY REFERENCES core.service_request,
    tier         text NOT NULL CHECK (tier IN ('strong', 'moderate', 'weak')),
    base_tier    text NOT NULL CHECK (base_tier IN ('strong', 'moderate', 'weak')),
    rule         text NOT NULL,
    keywords     text[] NOT NULL DEFAULT '{}',
    created_day  date,
    event_day    boolean NOT NULL,
    event_id     integer REFERENCES core.storm_event,
    confidence   real NOT NULL,
    rules_sha    text NOT NULL,
    classified_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS flood_signal_tier ON core.flood_signal (tier);
CREATE INDEX IF NOT EXISTS flood_signal_event ON core.flood_signal (event_id);
"""

# rough prior precision per base rule, refined after the sample review
CONFIDENCE = {"type_strong": 0.9, "type_moderate_kw": 0.85, "type_moderate": 0.5,
              "kw_only_strong": 0.6, "kw_only_weak": 0.3, "other_type_strong": 0.3}


def compile_list(pats):
    return [(p, re.compile(p, re.I | re.S)) for p in pats]


def storm_days(conn, cfg, typed):
    sd = cfg["storm_days"]
    rows = conn.execute("""
        SELECT created_local::date d, count(*) FROM core.service_request
        WHERE case_type = ANY(%s) AND created_local >= %s
        GROUP BY 1 ORDER BY 1""", (typed, sd["first_day"])).fetchall()
    counts = {d: n for d, n in rows}
    vals = sorted(counts.values())
    pct = vals[min(len(vals) - 1, int(sd["percentile"] / 100 * len(vals)))]
    threshold = max(pct, sd["min_count"])
    days = sorted(d for d, n in counts.items() if n >= threshold)

    merges = [(dt.date.fromisoformat(w["start"]), dt.date.fromisoformat(w["end"]), w["label"])
              for w in sd.get("merge_windows", [])]
    known = [(dt.date.fromisoformat(w["start"]), dt.date.fromisoformat(w["end"]), w["label"])
             for w in sd.get("known_events", [])]

    def merge_key(d):
        for a, b, lab in merges:
            if a <= d <= b:
                return lab
        return None

    events = []  # list of lists of days
    for d in days:
        if events:
            prev = events[-1][-1]
            same_window = merge_key(d) is not None and merge_key(d) == merge_key(prev)
            if (d - prev).days <= sd["max_gap_days"] + 1 or same_window:
                events[-1].append(d)
                continue
        events.append([d])

    ev_rows, day_rows = [], []
    for i, evdays in enumerate(events, 1):
        peak = max(evdays, key=lambda d: counts[d])
        label = merge_key(evdays[0]) or next(
            (lab for a, b, lab in known if a <= peak <= b), None)
        ev_rows.append((i, evdays[0], evdays[-1], peak, counts[peak], len(evdays), label))
        day_rows += [(d, counts[d], i) for d in evdays]
    return threshold, pct, counts, ev_rows, day_rows, known


def classify(conn, cfg, sha, event_of_day):
    t = cfg["types"]
    strong_t, mod_t, kwo_t, excl = (set(t["strong"]), set(t["moderate"]),
                                    set(t["keyword_only"]), set(t["excluded"]))
    kw_s = compile_list(cfg["keywords"]["strong"])
    kw_w = compile_list(cfg["keywords"]["weak"])
    kw_neg = compile_list(cfg["keywords"].get("negative", []))
    kw_void = compile_list(cfg["keywords"].get("void", []))
    kw_dem = compile_list(cfg["keywords"].get("demote", []))
    fields = cfg["text_fields"]
    text_sql = " || ' ' || ".join(f"coalesce({f}, '')" for f in fields)

    out = []
    stats = collections.Counter()
    with conn.cursor(name="cand") as cur:
        cur.itersize = 20000
        cur.execute(f"""
            SELECT case_number, case_type, created_local::date, {text_sql}
            FROM core.service_request
            WHERE NOT (coalesce(case_type, '') = ANY(%s))
              AND (case_type = ANY(%s) OR ({text_sql}) ~* %s)""",
                    (list(excl), list(strong_t | mod_t), PREFILTER))
        for cn, ctype, day, text in cur:
            stats["candidates"] += 1
            if any(rx.search(text) for _, rx in kw_void):
                stats["void"] += 1
                continue
            neg = any(rx.search(text) for _, rx in kw_neg)
            hs = [] if neg else [p for p, rx in kw_s if rx.search(text)]
            hw = [] if neg else [p for p, rx in kw_w if rx.search(text)]
            if neg:
                stats["negative_cancelled"] += 1
            if ctype in strong_t:
                base, rule = "strong", "type_strong"
            elif ctype in mod_t and hs:
                base, rule = "strong", "type_moderate_kw"
            elif ctype in mod_t:
                base, rule = "moderate", "type_moderate"
            elif ctype in kwo_t and hs:
                base, rule = cfg["keyword_only_strong_tier"], "kw_only_strong"
            elif ctype in kwo_t and hw:
                base, rule = "weak", "kw_only_weak"
            elif hs and cfg.get("other_type_strong_tier"):
                base, rule = cfg["other_type_strong_tier"], "other_type_strong"
            else:
                continue
            if not hs and any(rx.search(text) for _, rx in kw_dem):
                stats["demoted"] += 1
                if base == "weak":
                    continue
                base = TIERS[TIERS.index(base) - 1]
                rule += "+demoted"
            ev = None
            if day is not None:
                ev = event_of_day.get(day) or event_of_day.get(day - dt.timedelta(days=1))
            tier = TIERS[min(2, TIERS.index(base) + 1)] if ev else base
            out.append((cn, tier, base, f"{rule}:{ctype}", hs + hw, day, ev is not None, ev,
                        CONFIDENCE[rule.split("+")[0]] * (0.6 if "+demoted" in rule else 1.0), sha))
    return out, stats


def redact(s: str, n: int) -> str:
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", s)
    s = re.sub(r"\(?\b\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b", "[phone]", s)
    s = re.sub(r"\d{5,}", "[num]", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n] + ("…" if len(s) > n else "")


def write_sample(conn, cfg):
    sc = cfg["sample"]
    fields = cfg["text_fields"]
    rng = random.Random(sc["seed"])
    rows = []
    for tier in ("strong", "moderate", "weak"):
        cns = [r[0] for r in conn.execute(
            "SELECT case_number FROM core.flood_signal WHERE tier=%s ORDER BY case_number", (tier,))]
        pick = sorted(rng.sample(cns, min(sc["per_tier"], len(cns))))
        q = f"""SELECT f.case_number, f.tier, f.base_tier, f.rule, f.event_day, to_char(s.created_local, 'YYYY-MM'),
                       {", ".join("s." + x for x in fields)}
                FROM core.flood_signal f JOIN core.service_request s USING (case_number)
                WHERE f.case_number = ANY(%s)"""
        for r in conn.execute(q, (pick,)):
            parts = [f"{f}: {v}" for f, v in zip(fields, r[6:]) if v]
            rows.append([f"{tier[0].upper()}{len(rows) % 100 + 1:03d}", *r[:6],
                         redact(" | ".join(parts), sc["max_chars"]) or "(no text)", ""])
    SAMPLE.parent.mkdir(exist_ok=True)
    with SAMPLE.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sample_id", "case_number", "tier", "base_tier", "rule", "event_day",
                     "created_month", "text_redacted", "reviewer_relevant_yn"])
        w.writerows(rows)
    return len(rows)


def report(conn, cfg, sha, threshold, pct, counts, ev_rows, known, stats):
    L = []
    w = L.append
    w("# Phase 2: flood-signal classification\n")
    w(f"Generated by `classify/classify.py` from `classify/rules.yaml` (sha256 `{sha[:12]}`), "
      f"{dt.datetime.now():%Y-%m-%d %H:%M}. Aggregates only.\n")
    tot = conn.execute("SELECT count(*) FROM core.flood_signal").fetchone()[0]
    w(f"**{tot:,} flood-signal cases** from {stats['candidates']:,} candidates "
      f"({stats['negative_cancelled']:,} had keyword matches cancelled by a negative pattern, "
      f"{stats['void']:,} voided, {stats['demoted']:,} demoted by maintenance-only text).\n")

    w("## By tier\n")
    w("| Tier | Cases | Of which upgraded by storm day | Located (source coords, pre-Phase 3) |")
    w("|---|---:|---:|---:|")
    for tier, n, up, loc in conn.execute("""
        SELECT tier, count(*), count(*) FILTER (WHERE tier <> base_tier),
               count(*) FILTER (WHERE (s.lat_src BETWEEN 29.45 AND 30.20 AND s.lon_src BETWEEN -95.95 AND -94.90)
                                   OR (s.x_src > 0 AND s.y_src > 0))
        FROM core.flood_signal f JOIN core.service_request s USING (case_number)
        GROUP BY 1 ORDER BY array_position(ARRAY['strong','moderate','weak'], tier)"""):
        w(f"| {tier} | {n:,} | {up:,} | {loc:,} ({100 * loc / n:.1f}%) |")
    w("")
    w("## By rule and case type (top 40)\n")
    w("| Rule : case type | Base tier | Cases | On storm days |")
    w("|---|---|---:|---:|")
    for rule, base, n, ev in conn.execute("""
        SELECT rule, base_tier, count(*), count(*) FILTER (WHERE event_day)
        FROM core.flood_signal GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 40"""):
        w(f"| {rule} | {base} | {n:,} | {ev:,} |")
    w("")
    w("## By year\n")
    w("| Year | strong | moderate | weak | total |")
    w("|---|---:|---:|---:|---:|")
    for y, s, m, wk, n in conn.execute("""
        SELECT extract(year FROM created_day)::int, count(*) FILTER (WHERE tier='strong'),
               count(*) FILTER (WHERE tier='moderate'), count(*) FILTER (WHERE tier='weak'), count(*)
        FROM core.flood_signal GROUP BY 1 ORDER BY 1"""):
        w(f"| {y} | {s:,} | {m:,} | {wk:,} | {n:,} |")
    w("")
    w("## Keyword hits\n")
    w("| Pattern | Cases |")
    w("|---|---:|")
    for k, n in conn.execute("SELECT unnest(keywords), count(*) FROM core.flood_signal GROUP BY 1 ORDER BY 2 DESC"):
        w(f"| `{k}` | {n:,} |")
    w("")

    sd = cfg["storm_days"]
    vals = sorted(counts.values())
    w("## Storm days and events\n")
    w(f"Daily count of requests with a strong or moderate case type, {sd['first_day']} onward: "
      f"median {vals[len(vals) // 2]}, p{sd['percentile']} = {pct}. "
      f"Threshold = max(p{sd['percentile']}, {sd['min_count']}) = **{threshold}/day**. "
      f"Consecutive storm days (gap ≤ {sd['max_gap_days']} day) form one event; merge windows: "
      + ", ".join(f"{m['label']} {m['start']}–{m['end']}" for m in sd.get("merge_windows", [])) + ".\n")
    w(f"{sum(e[5] for e in ev_rows)} storm days in {len(ev_rows)} events.\n")
    w("| Event | Start | End | Storm days | Peak day | Peak requests | Label |")
    w("|---:|---|---|---:|---|---:|---|")
    for e in ev_rows:
        w(f"| {e[0]} | {e[1]} | {e[2]} | {e[5]} | {e[3]} | {e[4]:,} | {e[6] or ''} |")
    w("")
    w("### Known events check\n")
    w("| Known event | Window | Max daily requests in window | Detected? |")
    w("|---|---|---:|---|")
    for a, b, lab in known:
        inwin = [counts.get(a + dt.timedelta(days=i), 0) for i in range((b - a).days + 1)]
        mx = max(inwin) if inwin else 0
        hit = any(e[1] <= b and e[2] >= a for e in ev_rows)
        w(f"| {lab} | {a} – {b} | {mx:,} | {'yes' if hit else '**no**'} |")
    w("")
    DOC.write_text("\n".join(L))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="also write the redacted review sample")
    args = ap.parse_args()
    raw = RULES.read_bytes()
    cfg = yaml.safe_load(raw)
    sha = hashlib.sha256(raw).hexdigest()
    typed = cfg["types"]["strong"] + cfg["types"]["moderate"]
    t0 = time.time()
    with connect() as conn:
        conn.execute(DDL)
        threshold, pct, counts, ev_rows, day_rows, known = storm_days(conn, cfg, typed)
        event_of_day = {d: ev for d, _, ev in day_rows}
        signals, stats = classify(conn, cfg, sha, event_of_day)
        with conn.transaction():
            conn.execute("TRUNCATE core.flood_signal, core.storm_day, core.storm_event")
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO core.storm_event VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                [(*e, sha) for e in ev_rows])
                cur.executemany("INSERT INTO core.storm_day VALUES (%s,%s,%s)", day_rows)
                with cur.copy("""COPY core.flood_signal (case_number, tier, base_tier, rule, keywords,
                                 created_day, event_day, event_id, confidence, rules_sha) FROM STDIN""") as cp:
                    for r in signals:
                        cp.write_row(r)
            # HFD incidents link to storm events by day; event ids are renumbered above, so re-link.
            if conn.execute("SELECT to_regclass('core.hfd_flood_incident')").fetchone()[0]:
                conn.execute("""UPDATE core.hfd_flood_incident h SET storm_event_id = d.event_id
                                  FROM core.storm_day d WHERE d.day = h.local_day""")
                conn.execute("""UPDATE core.hfd_flood_incident h SET storm_event_id = NULL
                                  WHERE NOT EXISTS (SELECT 1 FROM core.storm_day d WHERE d.day = h.local_day)""")
        conn.execute("ANALYZE core.flood_signal")
        report(conn, cfg, sha, threshold, pct, counts, ev_rows, known, stats)
        print(f"{len(signals):,} signals, {len(ev_rows)} storm events, threshold {threshold}/day, "
              f"{time.time() - t0:.0f}s", flush=True)
        if args.sample:
            print(f"sample: {write_sample(conn, cfg)} rows -> {SAMPLE}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
