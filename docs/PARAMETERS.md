# Parameter reference (as built)

Every tunable in floodroutes, with its current value, where it lives, what it does, and what to run after changing it. Values are as of 2026-09-26.

## After a change: what to run

| Code | Meaning |
|---|---|
| **R** | `graphhopper/rebuild.sh` on quad. Rebuilds the risk surface, custom model, surface overlay and routing graph, then deploys to gitano. ~2 min; public routing pauses a few seconds. |
| **C** | `python classify/classify.py`, then **R**. Also runs nightly, but the graph only updates on **R**. |
| **D** | `deploy/gitano/deploy.sh` on quad. Copies code, web pages and built files to gitano and restarts its services. |
| **Q** | `sudo systemctl restart floodroutes-api` on quad (the local copy). |
| **NG** | On gitano: copy `deploy/gitano/floodroutes.nix` to `/etc/nixos/floodroutes.nix`, run `nixos-rebuild switch`, commit in `/etc/nixos`. |
| **NQ** | On quad: copy `deploy/quad/floodroutes.nix` to `/etc/nixos/floodroutes.nix`, run `sudo nixos-rebuild switch`, commit in `/etc/nixos`. |
| **I** | `python geocode/build_address_index.py`, then **D**. |
| **G** | `python geocode/geocode.py --priority 0` (new flood signals), then **R**. |

Visitors see web-page changes after **D** and a page reload (a hard reload, or wait for the 5-minute cache).

---

## 1. Live flood reports from app users: `api/live_config.yaml`

Read when the API starts. After any change: **D** (and **Q** for quad).

| Parameter | Value | What it does |
|---|---|---|
| `max_accuracy_m` | 100 | Reports whose GPS accuracy is worse than this are refused. |
| `snap_max_m` | 60 | A report is snapped to the nearest road; refused if no road is this close. |
| `per_device_max` / `per_device_window_min` | 6 / 10 | At most 6 reports per device per 10 minutes (HTTP 429 beyond). |
| `cluster_radius_m` | 80 | Reports this close together describe the same flooded spot. |
| `area_radius_m` | 60 | Radius around a spot that routing treats as flooded. |
| `expire_water_min` | 120 | "Water on road" counts for 2 hours. |
| `expire_impassable_min` | 180 | "Impassable" counts for 3 hours. |
| `expire_clear_min` | 180 | "Water's gone" counts against older reports for 3 hours. |
| `close_min_devices` / `close_window_min` | 2 / 30 | "Impassable" from 2 different devices within 30 minutes **closes** the road. |
| `clear_min_devices` | 2 | "Water's gone" from 2 devices, after the last hazard report, clears the spot. |
| `priority_closed` | 0 | Closed road: the flood-aware route avoids it entirely. |
| `priority_impassable_single` | 0.05 | One device says impassable: strong penalty (~20× cost), not a closure. |
| `priority_water` | 0.1 | "Water on road": strong penalty (~10× cost). |
| `priority_disputed` | 0.25 | A hazard with one "Water's gone" after it. |
| `max_active_spots` | 200 | Cap on spots applied to routing (performance guard). |

Other live-report settings:
- **Report database:** `LIVE_DB` environment variable. On gitano, `/var/lib/floodroutes/live.sqlite` (set in `deploy/gitano/floodroutes.nix`, change with **NG**); on quad, `data/live.sqlite`. Stored per report: road point, kind, time, GPS accuracy, and a hashed device token. Nothing else about the reporter.
- **Admin, from quad:** `deploy/gitano/clear-reports.sh` clears all reports on the public site (e.g. between demo rehearsals); `… list` shows active spots; `… near LAT LON [RADIUS_M]` clears around a point (default 150 m).
- **Locally:** `python -m api.live_admin list | clear-all | clear-near …` against `data/live.sqlite`.
- **Direct route:** live reports affect only the flood-aware (blue) route. The grey direct route is deliberately left as a normal router would give it.

## 2. Map page and drive mode: `CFG` block at the top of the script in `web/index.html`

After any change: **D** (plus a page reload).

| Parameter | Value | What it does |
|---|---|---|
| `offRouteM` | 50 | Re-route when the car is this far off the blue route... |
| `offRouteMaxAccuracyM` | 60 | ...and the GPS fix is at least this good. |
| `rerouteMinGapS` | 5 | Minimum seconds between off-route re-routes (real drive). |
| `rerouteMinGapSimS` | 3 | Same, in simulation. |
| `rerouteEveryS` | 20 | Re-route this often anyway, to pick up new reports and conditions. |
| `arriveM` | 40 | "Arrived" within this many metres of the destination. |
| `voiceAheadM` | 400 | Announce a flood area or live report this far ahead (~a quarter mile). |
| `voiceTiers` | high, medium | History tiers that get voice alerts (low is silent). |
| `liveNearRouteM` | 40 | A live report counts as "on the route" within this distance. |
| `simSpeedMps` | 13.4 | Simulated car speed at 1× (~30 mph). |
| `simSpeeds` | 4, 8, 16 | Speed-up choices for simulation (8× default). |
| `simStepS` | 0.5 | Simulation update interval. |
| `locateMaxS` / `locateGoodM` | 6 / 25 | "My location": best fix within 6 s, or earlier once accuracy is 25 m. |
| `roughFixM` | 60 | Show "±N m" after My location when accuracy is worse than this. |
| `suggestDelayMs` / `suggestLimit` | 150 / 6 | Pause after typing before suggestions; number shown. |
| `reportsRefreshS` | 60 | Refresh the live-reports layer this often. |

Fixed behaviour in the same file:
- the phone layout applies at ≤ 700 px wide;
- simulation buttons are hidden on phones, but `?sim=1` and `?sim=ignore` links still work;
- voice uses the browser's built-in speech; the 🔊/🔇 choice is remembered in the browser.

## 3. Routing: GraphHopper (`graphhopper/config.yml`, `risk/config.yaml`)

| Parameter | Value | Where | What it does | After change |
|---|---|---|---|---|
| `graphhopper_priority.high / medium / low` | 0.05 / 0.25 / 0.6 | `risk/config.yaml` | Priority multiplier inside each history tier (0.05 ≈ 20× cost). Priority, not speed, so travel times stay honest. | **R** |
| `graphhopper_area_mode` | `encoded_value` | `risk/config.yaml` | How tiers reach GraphHopper: `encoded_value` (per-road value set at import, fast); `multipolygon` / `polygon` (areas checked at query time, ~10× slower). | **R** |
| `distance_influence` | 90 | GraphHopper's built-in `car.json` | Adds 90 s of cost per km, so near-ties go to the shorter route. | (built in) |
| profiles | `car` (CH + LM), `car_flood` (LM, shares car's landmarks) | `config.yml` | `car_flood` requests need `ch.disable=true`; the API adds it. | **R** |
| `prepare.lm.landmarks` / `threads` | 16 / 2 | `config.yml` | Landmark count and import threads. | **R** |
| `graph.encoded_values` | car_access, car_average_speed, road_access, road_class, road_environment, flood_risk | `config.yml` | `flood_risk` (0–3) is set per road by `graphhopper/plugin` at import. | **R** |
| `import.osm.ignored_highways` | footway, construction, cycleway, path, steps, pedestrian, bridleway | `config.yml` | Ways left out of the car graph. | **R** |
| `routing.snap_preventions_default` | tunnel, bridge, ferry | `config.yml` | Points never snap onto these. | **R** |
| Heap | quad `-Xmx4g`; gitano `-Xmx2g`; imports `GH_HEAP=4g` | `deploy/*/floodroutes.nix`, `rebuild.sh` | Java memory. | **NQ** / **NG** |
| Ports | GraphHopper 8989 (admin 8990), API 8086, all on localhost | `config.yml`, modules | | **R** + **NQ**/**NG** |

## 4. Risk surface: `risk/config.yaml`

After any change: **R**.

| Parameter | Value | What it does |
|---|---|---|
| `h3_resolution` | 10 | Hexagon size (~66 m edge, ~130 m across). |
| `tier_weights.strong / moderate / weak` | 3 / 1.5 / 0.5 | Weight of each flood signal by classification tier. |
| `half_life_years` | 8 | Older reports count less; a report 8 years old counts half. |
| `as_of` | null (today) | Fix the date the decay is measured from, for reproducible builds. |
| `recurrence.per_event` | 1.0 | Hex score × (1 + 1.0 × distinct storm events), so repeat flooding across storms dominates. |
| `max_signals_per_hex_day` | 3 | Caps duplicate reports of one puddle on one day. |
| `thresholds.low / medium / high` | 10 / 25 / 60 | Hex score cut-offs for the tiers; below 10 a hex is not a risk area. |
| `hfd_comments.enabled` | true | Use firefighter-reported locations. |
| `hfd_comments.min_score.cut_off / recurring / major_event` | 60 / 25 / 10 | Minimum hex score set by a firefighter location (high / medium / low). |
| `hfd_comments.sample_m` / `point_radius_m` | 15 / 60 | Sampling along mapped streets; radius around a mapped intersection. |
| `simplify_m` | 10 | Polygon simplification tolerance. |
| `min_hexes_per_polygon` | 1 | Smallest risk area kept. |

The firefighter comments themselves are in `risk/hfd_comments.yaml`: 26 place-specific items with station, quote, street names and severity. After editing: **R** (`rebuild.sh` re-maps them onto roads).

Surface overlay and maps, both regenerated by **R** (or run the script directly):
- **Web overlay** (`risk/surface_overlay.py`): `PX_M = 40` (pixel size), `SIGMA_M = 150` (smoothing), colour range `VMIN, VMAX = 1, 160`, extent lon −95.80 to −95.05, lat 29.52 to 30.12.
- **Report map** (`risk/risk_map.py`): `STEP = 0.0008` (~80 m grid), `SIGMA_M = 250`.

## 5. Classification of 311 requests: `classify/rules.yaml`

After any change: **C**.

| Parameter | Value | What it does |
|---|---|---|
| `text_fields` | description, latest_notes, conflict_notes, resolution_notes | Free text searched by keyword rules (Title is excluded; it only holds type, case number and address). |
| `types.strong` | Flooding, Low Area or Standing Water ROW | Case types that are strong signals on their own. |
| `types.moderate` | Drainage, inlet/storm-sewer/ditch types (12) | Moderate on their own; strong with a strong keyword. |
| `types.keyword_only` | Street Hazard, Street Condition, Pothole, … (10) | Count only when the text matches. |
| `types.excluded` | Water Leak, Sewer…, Floodplain, Billing, Storm Debris, … (30) | Never a signal. |
| `keyword_only_strong_tier` | moderate | Tier for a keyword-only type with a strong keyword. |
| `other_type_strong_tier` | null (off) | Tier for any other type with a strong keyword (~10% precise in review, so off). |
| `keywords.strong / weak / void / demote / negative` | regex lists | Flood words; ditch and drain words need a water word nearby; void drops "disregard" / "entered in error"; demote lowers maintenance-only text one tier; negative cancels "flood light" / "flood zone". |
| `storm_days.percentile` / `min_count` | 99 / 100 | A storm day has ≥ max(p99 of daily counts, 100) drainage/flooding requests (currently 153/day). |
| `storm_days.first_day` | 2012-01-01 | Start of the counting period (2011 lacks locations). |
| `storm_days.max_gap_days` | 1 | Storm days this close form one event. |
| `storm_days.merge_windows` | Harvey 2017-08-25..09-30; Imelda 2019-09-18..09-30 | Late reports merged into the storm. |
| `storm_days.known_events` | 12 labelled storms | Labels in the report only. |
| `sample.per_tier` / `seed` / `max_chars` | 100 / 311 / 500 | Review sample (`docs/classification_sample.csv`, kept local). |

Confidence priors per rule are in `classify/classify.py` (`CONFIDENCE`).

## 6. Address search and geocoding

| Parameter | Value | Where | What it does | After change |
|---|---|---|---|---|
| TIGER counties | Harris, Fort Bend, Montgomery, Galveston, Brazoria, Waller, Chambers, Liberty (TIGER 2025 ADDRFEAT) | `data/external/tiger/*.zip` | Address ranges for search. | **I** |
| Named places | neighbourhoods, universities, schools, hospitals, libraries, fire and police stations, parks, museums, airports, malls, stations | `POI_TAGS` in `geocode/build_address_index.py` | What place-name search finds. | **I** |
| Exact-point preference | within 500 m of the interpolated spot | `api/geocoder.py` | Use an OSM exact address instead of interpolation. | **D** |
| Reverse lookup | exact address ≤ 25 m; block range ≤ 150 m; named place ≤ 400 m; "near street" ≤ 500 m | `api/geocoder.py` | Label for a clicked point. | **D** |
| Same street in suggestions | ≤ 2 ZIPs | `api/geocoder.py` | Keeps suggestions varied. | **D** |
| `/geocode` limits | query ≤ 120 chars, ≤ 10 results | `api/app.py` | | **D** |
| 311 source geocoding | `DISAGREE_M = 100`; placeholders ≥ 2,000 records × ≥ 40 types; `CENSUS_BATCH = 5000`; `--batch 5000` | `geocode/geocode.py` | X/Y vs lat/lon trust, placeholder detection (City Hall etc.), Census batch size. | **G** |

## 7. Public site: nginx on gitano (`deploy/gitano/floodroutes.nix`)

After any change: **NG**.

| Parameter | Value | What it does |
|---|---|---|
| Routing rate limit | `/route`, `/reverse`: 3 req/s per visitor, burst 20 | Excess gets HTTP 429. |
| Search rate limit | `/geocode`: 10 req/s, burst 40 | |
| Report rate limit | `/reports`: 2 req/s, burst 10 | On top of the per-device limit. |
| Tile cache | `/tiles/`: 20 GB max, entries idle 60 days evicted, OSM cache headers honoured (7-day fallback, 404s 1 h) | Tiles fetched from OSM only on view; identifying User-Agent. |
| Access log | `/var/log/nginx/floodroutes.log`: path only, visitor address truncated to /24 (/48) | Typed addresses and positions never reach disk. |
| API workers | 4 uvicorn workers, access log off | ~37 random city trips/s measured; GraphHopper is the limit. |
| Login | off (public). To require one: `basicAuthFile = "/var/lib/floodroutes/htpasswd";` | Credentials in quad's `.env` (`FLOODROUTES_WEB_USER` / `_PASSWORD`); `deploy.sh` writes the hash. |
| TLS | Let's Encrypt via NixOS ACME, renews automatically | |

## 8. API constants: `api/app.py`

After any change: **D** (and **Q**).

| Parameter | Value | What it does |
|---|---|---|
| `BBOX` | lon −96.2 to −94.7, lat 29.2 to 30.4 | Accepted points (the graph extent); outside gets 422. |
| `MAX_LIVE_AREAS` | 200 | Cap on areas sent with `POST /route`. |
| `GRAPHHOPPER_URL` (env) | http://127.0.0.1:8989 | Router address. |
| `LIVE_DB` (env) | see §1 | Live report database. |

## 9. Data pipeline and schedules (quad)

| Parameter | Value | Where | After change |
|---|---|---|---|
| Nightly refresh | 23:30 (+ up to 10 min random): download MTD + current-year YTD, load, classify, geocode new flood signals | `deploy/quad/floodroutes.nix` (`OnCalendar`) | **NQ** |
| Raw snapshots kept | newest 2 per source | `refresh.sh` (`--keep 2`) | none |
| Graph rebuild | on demand (`rebuild.sh`); weekly timer is on the to-do list | | |
| Database | `floodroutes-db`, PostGIS 18-3.6, 127.0.0.1:5442, data `/new/floodroutes/pgdata`, password in `.env` | `docker-compose.yml` | `docker compose up -d` |
| Deploy switches | `DEPLOY=0` (skip deploy in `rebuild.sh`), `DEPLOY_HOST` (default gitano), `GH_HEAP` (default 4g) | `rebuild.sh`, `deploy.sh` | none |

## 10. Validation and analysis scripts

| Script | Parameters |
|---|---|
| `validate/validate.py` | `--pairs 200`; trips ≥ 2 km apart; sample `REPEATABLE(25)`, seed 25; hotspot trips walk 0.7 / 1.2 / 2 km along roads. |
| `validate/hfd_stations.py` | Scores within 300 m and 1 km of each station, on the 311-only score. |
| `risk/coverage_stats.py` | City limits and roads from the OSM extract. |
| `ingest/recon.py` | Houston bbox lon −95.95 to −94.90, lat 29.45 to 30.20. |
