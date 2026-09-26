# floodroutes: Houston flood-aware routing

A routing server for Houston that steers drivers away from streets with a history of flooding and drainage problems. The history comes from the City of Houston 311 archive (2011–present plus the Harvey extract).

For each trip it returns two routes side by side:
- **`car`**: the normal car route.
- **`car_flood`**: the same car profile, with road priority lowered inside historical flood hotspots.

The flood data only lowers priority; it never removes a road.

**Map page:** http://127.0.0.1:8086/ (quad, localhost only). Layers: routing risk areas, smoothed risk surface, bayous. Deep links: `/?from=lat,lon&to=lat,lon`, `view=lat,lon,zoom`, `surface=1`.

> *Avoids streets with a history of flooding. Absence of reports does not mean a street is dry. Turn around, don't drown.*

## How the flood-aware route decides

**Headline: avoiding Houston's flood history costs about half a minute on a typical trip.** Over 200 random city trips:
- median +27–31 s (+2–3%), with 90% under about +2 minutes and none more than 50% longer;
- about 80% of the trips whose normal route crosses a high-risk area avoid high-risk areas entirely on the flood-aware route.

**Why detours are small**
1. **Hotspots are small and the street grid is dense.** A high-risk hex is ~130 m across and 0.7% of the city is high risk. There is almost always a parallel street a block away, so going around usually costs tens of metres and tens of seconds.
2. **The router adds only as much time as it must.** Inside a risk tier, a road segment's *priority* is multiplied by 0.05 (high), 0.25 (medium) or 0.6 (low). A high-risk segment therefore "costs" 20× its normal value: a 100 m flooded stretch weighs as much as ~2 km of ordinary road. The router takes any detour cheaper than that, choosing the one that adds the least time.
3. **Reported times are honest dry-road times.** The penalty changes which roads are *chosen*, never the speed assumed on them. "+23 s" is what the detour costs on a normal day. On a flood day the direct route may be crawling or impassable, so the real benefit is larger.
4. **History never closes a road.** If the only way across is, say, a bayou bridge with the next crossing 2 km away, the router accepts the hotspot rather than send the driver on a long detour. Two of the six validation hotspot trips behave this way, by design (`docs/validation.md`). Closing roads is the job of live data (`POST /route` `areas`: gauges, TranStar sensors), which are hard exclusions.

**How it's built**
- **Profiles:** each profile is a stack of GraphHopper "custom models".
  - `car` is GraphHopper's built-in `car.json`: roads a car may not use get priority 0; speed is each segment's average car speed derived from OSM tags at import; and `distance_influence: 90` adds 90 s of cost per km, so near-ties go to the shorter route. That is why a few detours show *negative* added time with positive added distance.
  - `car_flood` is `car.json` plus `flood_risk.json`: three rules on the per-segment `flood_risk` value (0–3), which the import plugin stores by testing each segment's geometry against the risk-tier polygons.
- **Speed-ups:** two precomputations, each suited to one profile.
  - **CH (Contraction Hierarchies)** ranks junctions by importance and adds ~900k shortcut edges, so a `car` query touches a few hundred junctions (~4 ms). But the costs are baked into the shortcuts, so CH can't serve a profile whose costs change per request.
  - **LM (Landmarks)** precomputes costs to and from 16 landmark points and uses them as lower bounds to steer an A* search (~16 ms for `car_flood`). The bounds stay valid whenever costs only go *up*, as flood penalties and live exclusions do. So `car_flood` reuses `car`'s landmarks and accepts per-request live areas; those requests must send `ch.disable=true` (the API does).

**Tuning: a policy choice, not a technical one.** For a more cautious flood route, change `risk/config.yaml` and run `graphhopper/rebuild.sh`:
- stronger multipliers (e.g. high 0.01 = ×100);
- a lower `thresholds.low` (more of the city counts as risky);
- extending risk along whole street segments rather than just the reported blocks.

Each makes detours longer and more frequent. How much delay drivers will accept for how much avoided risk is best set with city staff.

## Pipeline

```
311 index page ──download.py──▶ data/raw/*.txt (timestamped, SHA-256 manifest)
                 ──load.py────▶ raw.service_request_line (text) ─▶ core.service_request (typed, 5.6M cases)
                 ──classify.py▶ core.flood_signal (strong/moderate/weak) + core.storm_event
                 ──geocode.py─▶ core.service_request.geom (source point / Census / failed)
                 ──build_risk.py▶ core.risk_hex (H3 res 10) ─▶ core.risk_polygon ─▶ risk/flood_risk_tiers.geojson
                 ──write_custom_model.py▶ graphhopper/custom_models/flood_risk.json (priority rules on flood_risk)
                 ──FloodImport (graphhopper/plugin)▶ graphhopper/graph-cache (car: CH+LM, car_flood: LM; flood_risk per edge)
                 ──api/app.py (FastAPI)──▶ /route, /risk.geojson, web/index.html
```

| Phase | Code | Output / doc |
|---|---|---|
| 0 Recon | `ingest/recon.py`, `recon_report.py` | `docs/recon.md` |
| 1 Ingest | `ingest/download.py`, `parse311.py`, `load.py`, `schema.sql`, `refresh.sh` | `raw.*`, `core.service_request` |
| 2 Classify | `classify/classify.py`, `classify/rules.yaml` | `core.flood_signal`, `docs/classification.md`, `docs/classification_review.md` |
| 3 Geocode | `geocode/geocode.py`, `geocode/report.py` | `docs/geocode_report.md` |
| 4 Risk | `risk/build_risk.py`, `risk/config.yaml` | `risk/flood_risk_tiers.geojson`, `core.risk_polygon` |
| 5 Routing | `graphhopper/config.yml`, `write_custom_model.py`, `plugin/` (flood_risk encoded value), `rebuild.sh` | GraphHopper on :8989 |
| 6 API + map | `api/app.py`, `web/index.html`, `tests/` | :8086 |
| Validation | `validate/validate.py` | `docs/validation.md`, `docs/validation/*.png` |

## Setup (quad)

1. **Dev shell:** `nix develop` gives JDK 21, osmium, osm2pgsql, GDAL, psql and a uv venv built from `pyproject.toml`. It also exports `PGHOST`/`PGPORT`/`PGPASSWORD`, so `psql` connects directly.
2. **Database:** `docker compose up -d` starts `floodroutes-db` (PostGIS 18-3.6) on `127.0.0.1:5442`, database `houston311`, with data in `/new/floodroutes/pgdata`. The password is in `.env` (gitignored); create it with `POSTGRES_PASSWORD=$(openssl rand -hex 24)`.
3. **Services:** `deploy/quad/floodroutes.nix` is the source of truth, copied to `/etc/nixos/floodroutes.nix` and imported from `configuration.nix`. It defines:
   - `floodroutes-refresh.timer`: daily at 23:30
   - `floodroutes-graphhopper.service`: :8989, admin :8990
   - `floodroutes-api.service`: :8086

   After editing it, copy it over and run `sudo nixos-rebuild switch`.
4. **First build from nothing:**
   ```
   python ingest/download.py && python ingest/load.py
   python classify/classify.py --sample
   python geocode/geocode.py --priority 0          # flood signals; the rest: --priority 1 (hours, resumable)
   curl -LO https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf   # into data/osm/
   osmium extract -b -96.2,29.2,-94.7,30.4 --strategy=complete_ways -o data/osm/houston.osm.pbf data/osm/texas-latest.osm.pbf
   graphhopper/rebuild.sh                           # risk surface + custom model + import (~1.5 min)
   ```
5. **From the laptop:** `ssh -L 8086:localhost:8086 quad`, then open http://localhost:8086/.

## Refresh cycle

- **Nightly** (`refresh.sh`, from the systemd timer):
  1. re-download the MTD and current-year YTD extracts, keeping the newest 2 snapshots on disk;
  2. load them (skipped if the SHA-256 is unchanged; newer extracts win per case);
  3. rebuild `core.flood_signal`;
  4. geocode new flood signals.

  Every step is idempotent. A failure sends `/usr/local/bin/alert`.
- **On demand** (`graphhopper/rebuild.sh`): rebuild the risk surface and custom model, compile the import plugin if its sources changed, then reimport the graph through `FloodImport` into a fresh directory and swap it in. Don't use `java -jar … import` directly: the stock importer doesn't know `flood_risk`. A failed import keeps the old graph. It stops and starts the GraphHopper service around the swap, so routing is down for about 90 s.

## Tunable parameters

### `classify/rules.yaml` (Phase 2)
| Key | Meaning |
|---|---|
| `text_fields` | Free-text columns searched by the keyword rules. `Title` is excluded: it only ever holds the type name, case number and address. |
| `types.strong` / `moderate` / `keyword_only` / `excluded` | Case-type lists. Excluded types never produce a signal. |
| `keyword_only_strong_tier` | Tier for a keyword-only type with a strong keyword (default `moderate`). |
| `other_type_strong_tier` | Tier for any other type with a strong keyword; `null` disables it (default; ~10% precise in review). |
| `keywords.strong` / `weak` | Python regexes. Weak ditch/drain words need a water word nearby. |
| `keywords.void` | Drop the record ("disregard", "entered in error"). |
| `keywords.demote` | Maintenance-only text (mowing, trash, dropped phones, missing covers, water mains) goes down one tier unless a strong keyword matched. |
| `keywords.negative` | Cancels keyword matches ("flood light", "flood zone"). |
| `storm_days.percentile`, `min_count`, `first_day` | Storm day = daily count of strong+moderate case types ≥ max(percentile, min_count). |
| `storm_days.max_gap_days`, `merge_windows` | Group storm days into events; force-merge windows (Harvey late reports). |
| `sample.per_tier`, `seed`, `max_chars` | Review sample (`docs/classification_sample.csv`; gitignored because it contains redacted free text). |

### `geocode/geocode.py` constants (Phase 3)
- `BBOX`: accepted area.
- `DISAGREE_M = 100`: above this lat/lon vs X/Y distance, X/Y is used and the gap logged.
- `PLACEHOLDER_MIN_N = 2000`, `PLACEHOLDER_MIN_TYPES = 40`: points repeated this often across this many case types are office placeholders (City Hall and two others) and are treated as missing.
- `CENSUS_BATCH = 5000`: records per Census request.

### `risk/config.yaml` (Phases 4–5)
| Key | Default | Meaning |
|---|---|---|
| `h3_resolution` | 10 | ~66 m hex edge, roughly one block face |
| `tier_weights` | 3 / 1.5 / 0.5 | per-signal weight by tier |
| `half_life_years` | 8 | time decay (the city does fix drainage) |
| `as_of` | null | fix the decay date for reproducible builds |
| `recurrence.per_event` | 1.0 | score × (1 + per_event × distinct storm events in the hex) |
| `max_signals_per_hex_day` | 3 | caps duplicate reports of one puddle |
| `thresholds.low/medium/high` | 10 / 25 / 60 | hex score cut-offs; hexes below `low` are dropped |
| `simplify_m` | 10 | polygon simplification tolerance |
| `graphhopper_priority.high/medium/low` | 0.05 / 0.25 / 0.6 | priority multiplier inside each tier |
| `graphhopper_area_mode` | encoded_value | `encoded_value`: tier stored per edge at import (fast); `multipolygon`: one custom-model area per tier checked at query time (~10× slower); `polygon`: one area per polygon |

After changing any of these, run `graphhopper/rebuild.sh`.

### `graphhopper/config.yml`
The GraphHopper 11.0 settings:
- `car` uses CH and LM; `car_flood` uses LM only, sharing car's landmarks via `preparation_profile: car`. That is valid because flood weights are always ≥ car weights.
- `car_flood` requests need `ch.disable: true`. The API sets it.
- `flood_risk` (0 none, 1 low, 2 medium, 3 high) is listed in `graph.encoded_values`. It is set per edge at import by `graphhopper/plugin` (Java: `FloodRiskRegistry`, `FloodRiskParser`, `FloodRiskIndex`, `FloodImport`). The parser tests each edge's own geometry against the tier polygons: the same check custom-model `areas` make at query time, done once. It's a stock `IntEncodedValueImpl`, so the unmodified GraphHopper server loads the graph.
- Heap is `-Xmx4g`, set in the systemd unit and `GH_HEAP` in `rebuild.sh`.

## API

- `GET /route?from=lat,lon&to=lat,lon` returns:
  - `baseline` and `flood_aware`: GeoJSON geometry, `distance_m`, `time_s`, and `hotspots_crossed`;
  - `detour`: added metres and seconds, plus percentages;
  - `risk_exposure_m`: metres driven inside each tier, for each route;
  - `hotspots_avoided`: tier, storm events, report count, year range and metres inside.
- `POST /route` takes `{"from": [lat, lon], "to": [lat, lon], "areas": <FeatureCollection>}`. `areas` (Polygon or MultiPolygon, up to 200) are **hard exclusions** (priority 0) on the flood-aware route only. This is the hook for live TranStar, NWS and HCFCD data. Only the fixture in `tests/fixtures/` exists so far.
- `GET /risk.geojson`: the tier polygons.
- `GET /risk_surface.png`, `/risk_surface.json`: the smoothed surface as a map overlay (`risk/surface_overlay.py`: 40 m pixels, 150 m smoothing, Web Mercator rows; regenerated by `rebuild.sh`).
- `GET /bayous.geojson`: major bayou channels from OSM.
- `GET /health`

Tests: `python -m pytest tests/ -q`. They need GraphHopper running, and they check that no case numbers or text keys appear in any response.

## Privacy

Resident free text (names, phone numbers, emails, account numbers) lives only in the database.
- The API, map, GeoJSON, logs and committed docs contain only aggregates: tiers, scores, report counts, storm-event counts and year ranges.
- The classification review sample is redacted, but it can still contain names in free text, so it is gitignored and stays on quad.

## Results

As of 2026-09-25; see the docs for detail.
- **Data:** 5.59M cases → 163,507 flood signals (36.5k strong, 118k moderate, 8.9k weak).
- **Storm events:** 25 detected (late Imelda reports merged). Every named flood event is caught. Beryl is correctly absent: 311 shows it as a wind storm (tree, debris and signal requests quadrupled; flooding and drainage requests stayed normal).
- **Geocoding:** 97.7% of flood signals located (target 95%). 3,555 failures have neither a point nor an address, mostly 2011 migration records.
- **Risk surface:** 4,095 polygons (483 high, 1,158 medium, 2,454 low) covering 107 km².
- **200 random city trips** (ranges over three runs; the sample shifts while the background geocoder adds points):
  - median +27–31 s (+2–3% time); none more than 50% longer;
  - of the ~130 trips whose normal route crosses a high-risk area, 76–82% avoid high-risk areas entirely on the flood-aware route;
  - API latency (both routes plus overlay, keep-alive client) median ~22 ms, p99 ~82 ms, after moving flood risk into an encoded value (was ~300 / ~850 ms).
- **Top-10 hotspots:** 6 got a test trip through them and 4 of those are avoided. The two not avoided are by design (no reasonable alternative). The other 4 hotspots sit where short test trips never pass (freeway frontage, cul-de-sacs). See `docs/validation.md`.

## How much of Houston is at risk

Computed by `risk/coverage_stats.py` (2026-09-25). City limits (1,588 km²) and roads (24,285 km drivable) come from the OSM extract.

| Tier | Area | Share of city area | Share of road length |
|---|---:|---:|---:|
| High | 11.1 km² | 0.70% | 0.89% |
| Medium | 27.0 km² | 1.70% | 2.12% |
| Low | 68.1 km² | 4.29% | 5.49% |
| **Any tier** | 106.3 km² | **6.69%** | **8.51%** |

| Road type | km | High | Medium | Low | Any tier |
|---|---:|---:|---:|---:|---:|
| Freeways | 1,542 | 0.15% | 0.58% | 1.72% | 2.5% |
| Major roads | 5,004 | 0.89% | 2.06% | 5.97% | 8.9% |
| Local streets | 7,475 | 1.70% | 4.00% | 9.56% | 15.3% |
| Service roads, driveways and lots | 10,265 | 0.42% | 1.02% | 2.86% | 4.3% |

**Reading these numbers:**
- The tiers mean "floods *repeatedly* and gets *reported* repeatedly". A hex needs a score ≥ 10, in practice reports from more than one storm, so routes don't avoid the whole city.
- The looser measure, "has ever had a flood or drainage report", is much larger: 34,800 hexes covering 587 km², roughly a third of the city (not clipped to city limits).
- History concentrates on neighbourhood streets, where residents call from. Freeways barely register because freeway flooding isn't reported through 311 (TranStar covers it).
- Bayou-side roads with few front doors (e.g. North/South Braeswood west of Hillcroft) are under-reported for the same reason.

**The tiers are local.** Rough rectangles, not official neighbourhood boundaries:

| Area | High | Medium | Low | Any tier |
|---|---:|---:|---:|---:|
| I-10 / Washington wedge (Woodcrest / Camp Logan) | 8.5% | 9.1% | 12.6% | 30.2% |
| Meyerland (Brays Bayou, just outside the Loop) | 3.5% | 7.2% | 9.7% | 20.4% |
| Memorial City | 0.7% | 2.0% | 3.0% | 5.7% |
| Southwest inside the Loop (Braes Heights, West U, Southside Place) | 0.4% | 1.1% | 4.1% | 5.6% |

## Known limitations and next steps

- **Latency (fixed 2026-09-25):**
  - `car_flood` now reads the per-edge `flood_risk` encoded value instead of evaluating polygons per edge: p99 went from 765 ms to 91 ms in GraphHopper.
  - The API also shares one HTTP client; a per-request client cost ~200 ms.
  - On quad, *new* localhost TCP connections sometimes take ~200 ms to connect (seen for both GraphHopper and the API; OpenSnitch's connection interception is the suspect, unconfirmed). Browsers reuse connections, so map users don't see it.
- **Live exclusions** (`POST /route` `areas`) still use query-time areas. That's fine for a handful of polygons; hundreds would bring the old slowness back.
- **Moderate tier precision (~50%):** it is dominated by the `Drainage` type, much of which is ditch maintenance. The event-recurrence factor limits the damage, since dry-day maintenance calls don't recur across storms.
- **To do if the city continues the project:**
  - Weekly graph rebuild on a timer. The nightly refresh already reclassifies and geocodes; history changes slowly.
  - Low-downtime rebuild: import the new graph while the old one keeps serving, then restart (~10 s gap instead of ~90 s).
  - Live gauge feed: HCFCD gauges plus a table of road segments by flood stage (e.g. North/South Braeswood when Brays Bayou leaves its banks), sent as live `areas`. Bayou-side roads have few front doors, so 311 under-reports them.
  - TranStar roadway flood warning sensors (underpasses) as live `areas`.
- **Out of scope (hooks left):** live TranStar high water, NWS storm reports, HCFCD gauges and rainfall, USGS high-water marks (`POST /route` `areas`); OsmAnd/GPX export; public deployment.
