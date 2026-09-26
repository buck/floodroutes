# Scaling for a flood event

**Idea:** run on the cheap VPS (gitano) day to day. As a storm approaches, start extra servers in the cloud, then shut them down afterwards. Cost follows demand.

Status: design only, not built. Numbers are measured where noted; cloud prices are rough and should be checked before use.

## 1. Where we are

| Item | Today |
|---|---|
| Public site | gitano (4 cores, 8 GB): nginx, 4 API workers, GraphHopper (2 GB heap) |
| Measured capacity | ~37 random city trips/s (≈ 20 km trips, both routes plus overlay). GraphHopper is the limit, at ~105 ms CPU per trip vs ~43 ms for the API. |
| Per-server state | none except live reports (`/var/lib/floodroutes/live.sqlite`) and the nginx tile cache |
| Built files per server | ~200 MB graph + ~100 MB address index + a few MB of GeoJSON, pages and code (`deploy/gitano/deploy.sh` already ships exactly these) |
| Map tiles | proxied and cached from tile.openstreetmap.org (OSM policy: fine for a demo, not for heavy public traffic) |

**Load per user:**
- Planning a trip: 1 route, plus address lookups.
- Drive mode: 1 route every 20 s (`CFG.rerouteEveryS`), i.e. 0.05 routes/s per driver.

So 1,000 drivers ≈ 50 routes/s.

## 2. What copies cleanly (stateless)

GraphHopper and the API need only read-only built files. A new server is:
1. an image (AMI or container) with Java, the API's Python, nginx and the built files;
2. booted: ~1 min for the instance, then ~10 s for GraphHopper to load the graph;
3. registered with a load balancer.

**Rough capacity:** an 8-core compute-optimized instance (e.g. AWS c7g.2xlarge) should do ~70–80 trips/s. It is GraphHopper-bound and scales with cores; measure before relying on it.

| Servers | Routes/s | Drivers at once (at 1 route / 20 s) | Approx. cost, on-demand |
|---:|---:|---:|---|
| 1 | ~75 | ~1,500 | ~$0.30/hour |
| 10 | ~750 | ~15,000 | ~$3/hour, ≈ $150 for a 48-hour event |

Spot instances are cheaper still; the servers are disposable, so interruption is tolerable. Hetzner or another cloud with an API works the same way and is usually cheaper than AWS for this.

## 3. What needs changing

### 3.1 Live reports: the one piece of shared state
Every server must see the same reports. Options, simplest first:
1. **gitano as the report authority.** Cloud servers `POST /reports` to gitano and fetch active spots from it. Each server caches the spot list for ~5 s (spots feed every route's custom model). Small code change in `api/live.py`: a `LiveStore` that reads over HTTP.
2. **A small managed database** (Postgres or Redis) that every server, gitano included, uses. `LiveStore` becomes a thin client. More robust; costs a few dollars a month or runs only during events.

Either way the report rules (`api/live_config.yaml`) stay identical.

### 3.2 Map tiles: the real bottleneck at scale
Build our own **vector tiles** from the Houston OSM extract (Planetiler → one PMTiles file, ~100–300 MB). Host it as a **static file on S3 behind CloudFront** and render it in the browser with MapLibre GL. Then:
- it costs next to nothing at any load, since it's static and on a CDN;
- OSM's volunteer servers are never involved;
- it enables the heading-up driver view (MapLibre rotates natively), already on the to-do list.

### 3.3 Load balancer, TLS, rate limits
- **Front door:** CloudFront (static page, tiles, layers) plus an Application Load Balancer for `/route`, `/geocode`, `/reverse` and `/reports`. Or a single ALB if tiles are elsewhere.
- **DNS during events:** `floodroutes CNAME` points at CloudFront/ALB. Short TTL (e.g. 60 s) on that record so the switch is quick.
- **Rate limits:** per-visitor limits move from nginx to AWS WAF rate-based rules, keeping today's values (`docs/PARAMETERS.md` §7).
- **Privacy:** ALB and CloudFront logs include query strings. Turn access logging off, or strip query strings, so visitors' addresses and positions still never reach disk.

## 4. Scaling on the weather, automatically

The National Weather Service publishes alerts free at `https://api.weather.gov/alerts/active?zone=TXZ213` (Harris County; add neighbouring zones as needed).

A small job on gitano (systemd timer, every 15 min):

| Condition | Action |
|---|---|
| Flood Watch issued for the area | warm up: set capacity to N (e.g. 2–4) |
| Flash Flood Warning / Flood Warning | scale to M (e.g. 10); CloudFront/ALB takes over DNS |
| Measured load | step scaling on ALB request count or CPU, within [N, max] |
| No alerts for ~12 h | scale to 0; DNS back to gitano |

Watches typically come hours ahead, far more than the ~1 minute a server needs. A manual override (a script like `clear-reports.sh`) lets a person scale up or down at any time.

## 5. Cheaper to fix than to scale

1. **Don't re-route every 20 s when nothing changed.** Add `GET /reports/version` (a counter that changes whenever any spot changes). Phones re-route only when it changes, or when off-route. Most of the flood-time load is the 20-second timer, so this could cut routing load ~10×.
2. **Use CH when no live report is near the route.** The static flood profile could have a CH preparation (~4 ms per route) and fall back to LM (~16 ms) only when a report is within the route's bounding box. With `flood_risk` as an encoded value, `car_flood` can be CH-prepared at import.
3. **Cache popular routes** for a few seconds (same origin/destination rounded to ~50 m). Many people leave the same stadium, school or hospital.

## 6. Resilience

- **quad (home server)** runs the data pipeline. It may lose power or internet in a real storm, as in Harvey, and that's acceptable: history doesn't change mid-event, and the public site doesn't depend on quad.
- **Live reports must not live at home.** gitano (a datacenter VPS) is fine; a managed database is better.
- **gitano** is a single point of failure for the default site. With the cloud setup, CloudFront + ALB is the event-time front door, and gitano becomes one member (or just the report authority).
- **Rehearse once before hurricane season:** scale to 2, flip DNS, run `validate/validate.py` and a 5-minute load test, flip back.

## 7. Suggested order of work

1. Vector tiles on S3/CloudFront (also removes the OSM-tile dependency day to day).
2. `/reports/version` and on-change re-routing (big load cut, small change).
3. Shared report store (gitano authority first).
4. Server image + launch template + load balancer; manual scale script.
5. NWS-triggered scaling job.
6. CH for `car_flood` when no report is near (optimization).

## 8. Open questions

- Which cloud and account, and who pays during an event (the city, a sponsor)?
- Target: how many simultaneous drivers should it handle (Harvey-scale: tens of thousands)?
- Does the city want an official hostname (e.g. a houstontx.gov subdomain)? That changes TLS and DNS ownership.
