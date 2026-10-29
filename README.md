# Urja Meter API

**A clean, documented, read-only API over the Urja Meter Ops portal, plus a
small web client built on top of it.**

Urja Meter Ops is an internal web portal that a Jaipur electricity
distribution utility's staff use to look up smart meters: what a meter is,
where it sits in the network, where it is physically, and how much energy it
has recorded. The portal has no API, so the only "integration" today is
copy-pasting from a browser. This project gives other engineers and programs
a proper API instead, so nobody has to touch the portal.

![Urja Meter Explorer: fleet map with one meter's consumption and voltage](docs/screenshot.png)

### Deliverables at a glance

| What | Where |
|---|---|
| Source code | [`app/`](app) (service), [`app/static/`](app/static) (web client), [`tests/`](tests) |
| How the portal actually works | [**PROTOCOL.md**](PROTOCOL.md) |
| OpenAPI 3.1 description of this API | [**openapi.json**](openapi.json), also browsable at `/docs` (Swagger UI) and `/redoc` when running |
| Install, run, sample requests | [Quick start](#quick-start), [Using the API](#using-the-api) |
| Assumptions, design decisions and trade-offs | [Assumptions](#assumptions), [Design decisions and trade-offs](#design-decisions-and-trade-offs) |
| What I left out, and what I'd improve | [Intentionally left out](#intentionally-left-out), [What I'd improve with more time](#what-id-improve-with-more-time) |
| Reflection | [Reflection](#reflection) |
| Optional extensions | Hierarchy, local index and scale note, freshness, robustness, bulk path and web client: all done, see [Optional extensions](#optional-extensions) |

---

## Quick start

You need **Python 3.9+** or **Docker**. The portal URL and the credentials
from the assignment are built-in defaults, so there is nothing to configure.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --port 8080
```

Or with Docker:

```bash
docker build -t urja-meter-api .
docker run --rm -p 8080:8080 urja-meter-api
```

Then:

* **Web client:** <http://localhost:8080/>
* **Interactive API docs:** <http://localhost:8080/docs>
* **From a terminal:**

  ```bash
  curl localhost:8080/health
  curl "localhost:8080/v1/meters?install_status=Faulty&feeder=F-007"
  ```

Run the tests. They use a fake portal, so they need no network.

```bash
.venv/bin/pytest
```

After changing routes or response models, regenerate `openapi.json`:

```bash
PYTHONPATH=. .venv/bin/python scripts/export_openapi.py
```

### Configuration

Everything is optional and read from environment variables. The service does
not read a `.env` file itself; copy [.env.example](.env.example) to `.env` and
pass it in:

```bash
set -a; source .env; set +a; .venv/bin/uvicorn app.main:app --port 8080   # local
docker run --rm -p 8080:8080 --env-file .env urja-meter-api                  # Docker
```

| Variable | Default | Meaning |
|---|---|---|
| `PORTAL_URL` | `https://urja-ops.flockenergy.tech` | Portal base URL |
| `PORTAL_EMAIL` / `PORTAL_PASSWORD` | assignment credentials | Portal login |
| `PORTAL_REQUESTS_PER_MINUTE` | `100` | Client-side throttle. The portal starts refusing at about 120. |
| `INDEX_TTL_SECONDS` | `600` | Age after which the fleet snapshot is refreshed |
| `READINGS_TTL_SECONDS` | `300` | How long a readings response is cached |
| `REQUEST_TIMEOUT_SECONDS` | `20` | Timeout for each call to the portal |
| `WARM_ON_STARTUP` | `1` | Load the snapshot at startup. `0` loads it on the first request instead. |

> **Port 8000 busy?** Any free port works with `--port`. The docs use 8080
> because 8000 is often taken by other local tools.

---

## Using the API

All endpoints are `GET` and return JSON. The full contract, with every
parameter and response field described, is in [openapi.json](openapi.json)
and at `/docs`.

| Endpoint | What it gives you |
|---|---|
| `GET /v1/meters` | Search, filter and page through meters. See the filters below. |
| `GET /v1/meters/{id}` | One meter: nameplate, full network path, location, data-quality notes |
| `GET /v1/meters/{id}/readings` | Readings in a date window (`from`, `to`, both inclusive). `resolution=raw` (default) gives every reading plus consumption per interval. `resolution=daily` gives kWh, kVAh, power factor and voltage per day. |
| `GET /v1/transformers` | All 40 distribution transformers with meter counts by status. Optional `feeder` filter. |
| `GET /v1/transformers/{code}` | One transformer |
| `GET /v1/hierarchy` | The network as a tree (zone → … → transformer) with a meter count at every node |
| `GET /v1/summary` | Fleet-wide counts by status, make, phase and install type |
| `GET /health` | `ok`, `degraded` (last refresh failed, serving older data) or `unavailable` |

**Filters on `/v1/meters`** (all optional, all combinable):

* `q`: substring of meter id or serial, case-insensitive
* `install_status`: `Installed`, `Faulty` or `Decommissioned`
* `make`: send `L&T` as `L%26T`
* `phase_type`: `single` or `three`
* `install_type`: `Whole Current` or `CT Operated`
* `has_data_quality`: `true` for meters whose portal data was repaired or
  flagged
* any network level by code: `zone`, `circle`, `division`, `subdivision`,
  `substation`, `feeder`, `dt`
* `near_lat`, `near_lng`, `radius_km`: meters near a point, nearest first
* `page`, `page_size`: page size up to 500

**Errors** always have the same shape:

```json
{ "error": { "code": "not_found", "message": "Meter J999 not found" } }
```

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | Unknown meter, transformer or route |
| 422 | `invalid_request` | Bad parameters (the message says which) |
| 502 | `portal_error` / `portal_auth_failed` | The portal sent something unexpected or rejected our login |
| 503 | `portal_unavailable` | The portal kept failing (rate limit, 5xx, network) after retries |

### Sample requests

**Faulty three-phase meters, one per page:**

```bash
curl "localhost:8080/v1/meters?install_status=Faulty&phase_type=three&page_size=1"
```

```json
{
  "data": [
    {
      "meter_id": "J100005",
      "serial_no": "HP63682",
      "make": "Allied",
      "phase_type": "three",
      "install_status": "Faulty",
      "install_type": "Whole Current",
      "nameplate_format": "legacy",
      "hierarchy": {
        "zone": {"code": "Z-03", "name": "Jaipur Zone 3"},
        "circle": {"code": "C-06", "name": "Circle 6"},
        "division": {"code": "D-06", "name": "Division 6"},
        "subdivision": {"code": "SD-06", "name": "Subdivision 6"},
        "substation": {"code": "SS-06", "name": "Substation 6"},
        "feeder": {"code": "F-006", "name": "Feeder 6"},
        "dt": {"code": "DT-006", "name": "Jhotwara DT 6"}
      },
      "location": {"latitude": 26.84670538836401, "longitude": 75.86828425070057},
      "data_quality": [],
      "distance_km": null
    }
  ],
  "page": {"page": 1, "page_size": 1, "total": 35, "total_pages": 35},
  "freshness": {"snapshot_loaded_at": "2026-10-27T17:20:02.431780Z", "stale": false}
}
```

**Daily consumption for one meter over the last week of June:**

```bash
curl "localhost:8080/v1/meters/J100001/readings?from=2026-06-24&to=2026-06-30&resolution=daily"
```

```json
{
  "meter_id": "J100001",
  "resolution": "daily",
  "interval_minutes": 30,
  "window_start": "2026-06-24T00:00:00+05:30",
  "window_end": "2026-06-30T23:30:00+05:30",
  "total_kwh": 138.38,
  "daily": [
    {"date": "2026-06-24", "kwh": 19.82, "kvah": 21.42, "power_factor": 0.925, "intervals": 48,
     "voltage_r_min": 220.0, "voltage_r_max": 240.0, "voltage_r_avg": 228.7},
    "… one entry per day up to 2026-06-30 …"
  ],
  "data_quality": []
}
```

**More examples:**

```bash
# Meters within 1.5 km of a point, nearest first
curl "localhost:8080/v1/meters?near_lat=26.9124&near_lng=75.7873&radius_km=1.5"

# Meters whose portal data had to be repaired, with the reasons
curl "localhost:8080/v1/meters?has_data_quality=true"

# One branch of the network (codes below circle repeat, so give the parents too)
curl "localhost:8080/v1/meters?circle=C-01&division=D-01"
```

> The portal only holds readings for **June 2026**. A window outside that
> returns an empty result, not an error.

---

## The web client

The **Urja Meter Explorer** is served at <http://localhost:8080/>. It is built
only on the public `/v1` API, so it also serves as a worked example of using
the API.

* **Summary tiles.** Fleet size, status split and data-quality count. Click a
  tile to filter the map.
* **Map.** Every meter is coloured by status. Three-phase meters are drawn
  larger, and meters with data-quality notes are outlined. You can filter by
  search, status, make and phase. To move around, drag or scroll/two-finger
  swipe in any direction; to zoom, pinch or hold ⌘/Ctrl and scroll.
* **Meter panel.** Click a meter to see:
  * its nameplate and its path through the network;
  * any data-quality notes;
  * daily consumption with power factor (click a day for its half-hourly
    profile);
  * daily voltage against India's 216–253 V supply limits for 230 V
    consumers (+10% / −6%).

  The panel also flags meters marked Faulty or Decommissioned that are still
  reporting consumption.
* **Transformers.** Ranked by share of faulty meters, with a status bar per
  transformer. Click a row to see its meters on the map.
* **Network.** A collapsible zone → … → transformer tree with meter counts.
  Click any node to see its meters on the map. The filter uses the node's full
  path, because codes repeat under different parents.
* **Deep links.** `/ui/#meter=J100001` opens a meter directly, and
  `/ui/#view=dt-view` opens the transformers tab.

Map tiles come from OpenStreetMap, and Leaflet and Chart.js load from public
CDNs, so the browser needs internet access.

---

## How it's built

```
app/
  portal.py     Portal client: login and re-login, rate limiting, retries,
                HMAC-signed export, SvelteKit __data.json decoding.
                The only module that knows the portal's wire format.
  normalize.py  Pure functions: both nameplate formats, hierarchy parsing and
                repair, typed and de-duplicated readings, consumption, daily rollups.
  index.py      In-memory fleet snapshot: filtering, proximity search,
                hierarchy tree, summary, background refresh.
  schemas.py    Pydantic response models (these also generate openapi.json).
  main.py       FastAPI routes, validation, error mapping, readings cache.
  config.py     Settings from environment variables.
  static/       The web client: one HTML file, no build step.
tests/          Normalisation unit tests, client tests against a mock portal,
                API tests against a fake portal.
scripts/        export_openapi.py writes openapi.json from the code.
```

**How a request is answered:**

* **Meters, transformers, hierarchy and summary** come from an **in-memory
  snapshot** of the whole fleet.
  * It is built from the portal's signed bulk export (one request for all 403
    meters) plus the transformer list (two requests), and loaded at startup.
  * Once it is older than 10 minutes, requests are still answered from it
    straight away while one background task refreshes it.
  * Every response includes a `freshness` block saying when the snapshot was
    loaded and whether it is stale.
* **Readings** are fetched **live** from the portal, normalised, and cached
  for 5 minutes. Identical requests arriving together share one portal call.
* **A meter that isn't in the snapshot** (e.g. added since the last refresh)
  is fetched live from the portal's meter page and location endpoint. The
  response says `"source": "live"`.

---

## Assumptions

* **Timezone.** Portal timestamps carry no zone. I treat them as Asia/Kolkata
  (+05:30), since this is a Jaipur utility, and return ISO 8601 with the
  offset.
* **kWh and kVAh are cumulative registers.** Consumption is the difference
  between consecutive readings. A decrease is treated as a reset or bad read:
  it is excluded from consumption and flagged.
* **Daily totals** count each interval on the day it *starts*. The last day
  of a window can therefore be partial; its `intervals` field says how many
  intervals it covers (48 = a full half-hourly day).
* **The transformer register is authoritative for transformer names.** When a
  meter reports a different name (the "Old Malviya Nagar Xfmr" case), the
  meter keeps what the portal said and gets a flag.
* **Repairs are inferred from the rest of the fleet.**
  * A blank code is filled with the code most other meters pair with that
    name, and a blank name the same way from its code.
  * Every repair is listed in `data_quality`, so a consumer who disagrees can
    see it and ignore it.
* **Read-only.** The API is read-only, matching what the portal exposes to
  this account.
* **A few minutes of staleness is fine for meter metadata.** Status and
  network position change on the timescale of field visits, not seconds.
  Readings are never served from the snapshot.

## Design decisions and trade-offs

* **Snapshot and index, instead of forwarding every request to the portal.**
  The portal can only search by id or serial and allows about 120 requests a
  minute. Answering "faulty meters on feeder F-007" or "meters near here"
  live would take hundreds of calls; the bulk export makes a full snapshot
  cost 3.
  * *Cost:* metadata can be up to `INDEX_TTL_SECONDS` old, and every response
    says how old.
  * *If a refresh fails,* the old snapshot keeps being served with
    `stale: true`, and the next attempt waits 30 seconds so an outage doesn't
    turn every request into another hit on the portal.
  * *With no snapshot yet,* requests during that wait get an immediate 503
    rather than each sitting through retries.
* **Use the bulk export rather than per-meter pages for metadata.**
  * It is one call instead of about 800, and it is already structured.
  * It is the same path the portal's own "Export all meters" button uses.
  * The per-meter path is still implemented and tested, as the fallback for
    meters missing from the snapshot. That is also where the two nameplate
    formats are handled.
* **Readings are live, not crawled ahead of time.** Pre-fetching every
  meter's readings would cost about 4 minutes of rate-limit budget, mostly
  for data nobody asks for. A short cache covers repeat requests.
* **Protect the portal.**
  * Requests are spaced out so the rate limit is never hit, with backoff as
    the safety net.
  * When the 1-hour session expires, concurrent requests share one re-login.
  * Meter ids are validated before they go into portal URLs, so a value like
    `..` can't reach other portal paths.
* **Report data problems instead of hiding them.** Each repair or anomaly is
  listed in `data_quality`. Consumers get clean data *and* the reasons, which
  matters when a utility acts on it.
* **The hierarchy is a tree of paths, not of codes.** Below circle level the
  portal reuses codes under different parents (D-01 sits under three
  circles). Keying nodes by code alone would wrongly merge branches.
* **Python and FastAPI, no database.** 403 meters fit easily in memory.
  FastAPI generates the OpenAPI document from the same models that validate
  responses, so the spec can't drift from the code.
* **Versioned paths (`/v1`)**, so the response shape can change later without
  breaking existing consumers.

## Intentionally left out

* **Authentication on this API.** It runs as one shared portal account. In
  production it would sit behind the company's gateway or SSO, which this
  project shouldn't reinvent.
* **Credentials outside the code.** The defaults in `config.py` exist so a
  reviewer can run it with zero setup. In production they would come only
  from a secrets manager.
* **Persistent storage.** A restart rebuilds the snapshot in about 2–3
  seconds.
* **Running several copies at once.** The rate limiter is per process, so two
  replicas would share the portal's budget without knowing it.
* **A front-end build pipeline.** The web client is one HTML file using
  Leaflet and Chart.js from a CDN, so there is nothing to install or build. A
  bigger client would justify React/Vite and its own tests.
* **Fleet-wide consumption analytics** (e.g. "top 10 consumers"). This needs
  every meter's readings, which means a background crawl under the rate
  limit.

## Scale: where the local index would start to struggle

I measured the index with 100,000 synthetic meters (the real data repeated):

| | 403 meters (today) | 100,000 meters |
|---|---|---|
| Memory | ≈1 MB | ≈270 MB (≈2.6 KB per meter) |
| Filter by status + feeder | < 1 ms | 28 ms |
| Text search | < 1 ms | 14 ms |
| "Within 2 km" search | < 1 ms | 60 ms |
| Build hierarchy tree | < 1 ms | 75 ms, once per refresh |

* **Up to about 100k meters** this design is fine.
* **Beyond that,** memory and linear scans become the problem. I would move
  the snapshot into **SQLite or Postgres**, with indexes on status, make and
  each network code, and a spatial index (PostGIS, or a geohash) for
  proximity queries.
* **Refreshing would have to change too.** A single export call returning
  millions of rows isn't realistic. I'd page through it, or refresh
  incrementally if the portal ever offers "changed since".
* **For readings at scale,** a scheduled crawler would write into a
  time-series store (e.g. TimescaleDB). The API would read from that, and the
  portal's rate limit would only affect how fresh that store is.

## What I'd improve with more time

* **A background readings crawler** that stores every meter's readings, so
  fleet-wide analytics and anomaly detection become possible. Examples:
  registers that stop moving, voltage excursions, meters marked Faulty that
  are still reporting.
* **A full reconciliation job** comparing the export with the per-meter
  pages, instead of my spot check.
* **Structured logs and metrics.** Portal latency, 429 count and snapshot age
  are the three numbers that would tell you this service is unhealthy.
* **Opt-in contract tests against the live portal**, so a change to its
  undocumented endpoints is caught before consumers notice.
* **Remembering unknown meter ids.** A miss in the snapshot triggers a live
  portal lookup, so a client asking for many non-existent ids spends
  rate-limit budget. A short "not found" cache would fix that.

## Optional extensions

| Extension | Status |
|---|---|
| Network hierarchy | `/v1/hierarchy` and network filters. Blank codes and names are repaired and flagged, reused codes handled. |
| Local index / query layer | Combined filters and proximity search the portal can't do, plus the [scale note](#scale-where-the-local-index-would-start-to-struggle). |
| Freshness / caching | 10-minute snapshot with background refresh, 5-minute readings cache, `freshness` in every response. |
| Robustness | Re-login on expiry, proactive throttling, backoff, stale data served during outages, single re-login under load. |
| The full dataset | The signed bulk export ([PROTOCOL.md](PROTOCOL.md#the-bulk-export-and-its-signature)). |
| Modern web client | The [Urja Meter Explorer](#the-web-client). |

---

## Reflection

**What assumptions did you make?**

* The portal's undocumented `/portal/*` endpoints are fair to depend on.
  They're what the portal's own front-end calls, and far more robust than
  scraping HTML.
* Timestamps are local Jaipur time.
* `kwh` and `kvah` are cumulative registers, since they only ever increase.
* The transformer register is a better source of names than individual meter
  records.
* A few minutes of staleness on meter metadata is acceptable.

Each of these is written down above and visible in the API (`freshness`,
`data_quality`, offsets on timestamps), so a consumer who disagrees can tell.

**Which part was the most difficult, and how did you get unstuck?**

The first obstacle was that the meters page showed "0 total" to a logged-in
user, and nothing in the HTML said where the data came from. Recognising
SvelteKit was the way in: the `modulepreload` headers led to the JavaScript
for each page, and that contained every `fetch()` call, including the signed
export.

The subtler difficulty was the data itself. Readings looked uniform until I
pulled a hundred meters and saw that some were daily, not half-hourly. The
hierarchy looked like a tree until I counted parents per code. Both times,
what got me unstuck was profiling the whole dataset instead of trusting the
first few records.

**If you had another day, what would you improve?**

* A throttled background crawler that stores all readings. That would make
  fleet-wide questions answerable, such as listing every meter marked Faulty
  or Decommissioned that is still reporting consumption. Today the web client
  shows this one meter at a time; all three such meters I checked had it.
* Operational metrics: snapshot age, portal latency and 429s.

**What mistake did you make while solving this?**

I pulled readings for every meter with 8 parallel workers before I knew the
portal had a rate limit. 102 requests succeeded, 301 were refused, and I was
locked out for about half a minute. It was a useful accident, because it is
how I measured the limit. But I should have probed with a small, slow burst
before going parallel against someone else's production system. The service
now throttles itself for exactly that reason.

**If you were reviewing your own submission, what would you criticise?**

* **The rate limiter is per process.** Running two replicas would quietly
  double the load on the portal.
* **The hierarchy repair is a majority vote.** It is right for this data, but
  if the blank values were the majority it would confidently repair in the
  wrong direction.
* **Iterating over readings is still slow.** A consumer looping over all 403
  meters' readings would wait about 4 minutes. I document that rather than
  solve it.
* **The tests mock the portal as I observed it.** If the portal changes, the
  tests will still pass while production breaks. That is why live contract
  tests are on the improvement list.
* **The web client has no automated tests.** I checked it by rendering it in
  a headless browser against the live portal, not with a test suite.
