# How the Urja Meter Ops portal actually works

This is what I found by using the portal, reading its JavaScript bundles and
probing its endpoints with `curl` and small Python scripts. Everything here was
observed against `https://urja-ops.flockenergy.tech` on 27 Oct 2026.

## The short version

The portal is a **SvelteKit** app. The HTML pages are almost empty: the
browser fetches JSON from a handful of undocumented `/portal/*` endpoints and
renders it. So there *is* an API, just not a documented or consistent one. My
service talks to those endpoints directly, never scraping HTML.

| What | Endpoint | Notes |
|---|---|---|
| Log in | `POST /login` (form: `email`, `password`) | SvelteKit form action. Needs an `Origin` header. Sets the session cookie. |
| Log out | `POST /api/auth/sign-out` | [better-auth](https://www.better-auth.com/) |
| Search meters | `GET /portal/meters/search?q=&page=` | 20 per page, fixed. Matches id or serial only. |
| Meter page data | `GET /meters/{id}/__data.json` | SvelteKit server-load data, `devalue`-encoded. Nameplate + hierarchy. |
| Meter location | `GET /portal/meters/{id}/geo` | Lat/long as strings. |
| Meter readings | `GET /portal/meters/{id}/energy?from=&to=` | Undocumented `from`/`to` (YYYY-MM-DD). |
| Transformers | `GET /portal/dts?page=` | 20 per page, 40 in total. |
| Signing secret | `GET /portal/keys` | Per-session HMAC secret for the export. |
| **Bulk export** | `GET /portal/export?page=1` + signature headers | **Every meter with hierarchy and geo in one call.** |

## How I found it

1. `GET /` redirects to `/login`. The response headers (`x-sveltekit-page`)
   and the `_app/immutable/...` asset paths gave away SvelteKit.
2. After logging in, `/meters` rendered "0 total / No meters found". The
   server isn't rendering data, so the browser must fetch it.
3. The page's `<link rel="modulepreload">` headers list the JS chunk for each
   route. `entry/app.*.js` contains the route table (`/meters`,
   `/meters/[id]`, `/transformers`). The route chunks (`nodes/*.js`) contain
   the `fetch()` calls, including the export button's signing code on the
   transformers page.
4. From there it was a matter of calling each endpoint and varying the
   parameters to see what they accept.

## Authentication

* `POST /login` with form fields `email` and `password`. Without an
  `Origin: https://urja-ops.flockenergy.tech` header SvelteKit rejects it as a
  cross-site form post.
* On success: `{"type":"redirect","status":303,"location":"/meters"}` and a
  cookie `__Secure-better-auth.session_token` with **Max-Age 3600 (1 hour)**.
* On bad credentials: HTTP 200 (!) with
  `{"type":"failure","status":401,...,"Invalid email or password."}`.
  You have to read the body, not the status code.
* An expired or missing session looks different depending on the endpoint:
  * `/portal/*` JSON endpoints: `401 {"error":"unauthorized"}`
  * `__data.json` endpoints: **`200 {"type":"redirect","location":"/login"}`**

  The client handles both: it logs in again once and retries.

## The bulk export and its signature

The "Export all meters" button on the Transformers page calls
`/portal/export`, which needs two headers:

```
x-timestamp: <unix seconds>
x-signature: hex( HMAC-SHA256( secret, "GET\n/portal/export\npage=1\n<timestamp>" ) )
```

`secret` comes from `GET /portal/keys` → `{"data":{"signingSecret":"..."}}`.
It is fetched per session. Without a valid signature you get
`401 {"error":"signature_invalid"}`.

The export is the best source in the portal:

* **All 403 meters in one response**, regardless of `page` (`pageSize` is
  ignored). It returns `total`, so a short page would be detectable.
* Hierarchy as structured `{name, code}` objects rather than `"Name (CODE)"`
  strings.
* Geo as numbers rather than strings.
* A `build` field (`legacy` / `v2`) that explains the two nameplate formats below.

I spot-checked it against the per-meter endpoints for a handful of meters
(J100000, J100001, J100004): same values. That is a spot check, not a full
reconciliation.

## Data model

```
Zone → Circle → Division → Subdivision → Substation → Feeder → DT (distribution transformer) → Meter
```

* **403 meters**, `J100000`–`J100402`.
* **40 transformers**, `DT-001`–`DT-040`, each with a feeder code and a
  capacity in kVA.
* Meter attributes: serial number, make (HPL, Genus, Secure, Allied, L&T),
  phase (`single`/`three`), status (`Installed`/`Faulty`/`Decommissioned`),
  installation type (`Whole Current`/`CT Operated`), location.

## Readings

`GET /portal/meters/{id}/energy`

* Returns `{"data":[{"timestamp","kwh","kvah","voltR"}, ...]}`.
* **Everything is a string**, including the numbers. Blank values are `""`.
* **Timestamps are `DD/MM/YYYY HH:MM`** with no timezone. I assume local
  Jaipur time (IST, +05:30), since the utility is in Jaipur and readings run
  from 00:00.
* `kwh` and `kvah` are **cumulative register readings**, not consumption. You
  have to take differences to get usage.
* **Undocumented window parameters:** `from=YYYY-MM-DD&to=YYYY-MM-DD`, both
  days inclusive.
  * With neither, you get the last 7 days of available data.
  * With only `from`, you get 7 days from that date.
  * An unparseable `from` is silently ignored.
  * `from > to` returns an empty list rather than an error.
  * Other names (`start`, `startDate`, `days`, `limit`…) are ignored.
* **Data only exists for June 2026** (1–30 June). Anything outside returns `[]`.
* **The cadence differs per meter.** Some meters report every 30 minutes
  (1440 readings for June), others **once a day** (30 readings). In my sample,
  J100000–J100039 were half-hourly and J100040 onwards daily. It does not
  correlate with make, phase, install type or `build`. The API therefore
  detects and reports `interval_minutes` per meter instead of assuming one.
* **Duplicates:** J100089 has two rows for `30/06/2026 00:00`. The second has
  blank `kwh` and `voltR`. I keep the more complete row and report it.
* The UI renders blanks as "—", so blank values are clearly expected.

## Quirks and data-quality problems

| Problem | Where | How the API handles it |
|---|---|---|
| **Two nameplate formats.** `legacy` meters: `detail.data` = list of `{parameterName, parameterValue}` with labels like "Serial No". `v2` meters (165 of 403): `detail.classData` = a **JSON string** containing `{"installed_meter": {"SerialNo": ...}}` (PascalCase). | `__data.json` | Both are parsed into the same fields (`normalize.parse_nameplate`). |
| **`devalue` encoding.** `__data.json` is a flat array where values point to other indices. | `__data.json` | Small decoder in `portal.decode_devalue`. |
| **Hierarchy as strings.** `"Jaipur Zone 1 (Z-01)"` on the meter page. | `__data.json` | Parsed into `{name, code}`. |
| **Blank hierarchy codes or names.** 22 meters have a level with an empty `code` (e.g. `{"name":"Circle 6","code":""}`) or an empty `name`. They affect circle, substation and feeder levels. | export | Repaired from the rest of the fleet (majority code↔name mapping). Every repair is listed in the meter's `data_quality`. All 22 were repairable. |
| **Codes are reused under different parents.** Zone→circle is consistent (each circle belongs to one zone). Below that it isn't: division `D-01` appears under circles C-01, C-03 and C-05, and `SD-01` under three divisions. | export | Not "fixed": it may be how the utility numbers things, and I can't tell which parent is right. The hierarchy tree identifies nodes by **their full path**, not by code alone. Filters (`?division=D-01`) match by code and say so. |
| **Transformer name disagreement.** J100400–J100402 (the only `GN…` serials, all Genus three-phase) report DT-007 as **"Old Malviya Nagar Xfmr"**. The transformer register and the other 10 meters call it "Sanganer DT 7". They look like later additions carrying a stale name. | export | The meter keeps what the portal says. It is flagged in `data_quality`. `/v1/transformers` uses the register name. |
| **Status contradicts readings.** Meters marked `Decommissioned` (e.g. J100000) or `Faulty` (e.g. J100004, J100005) report normal, steadily rising consumption for the whole of June. Either the status field is out of date or the readings are synthetic/estimated. I checked a handful, not the whole fleet. | status vs energy | Passed through unchanged. The web client flags it on the meter's page as something to check. |
| **Serial prefixes don't match makes.** E.g. `SE…` serials on HPL meters, and serials like `L&84997`, which look like a mangled "L&T" prefix. | everywhere | Passed through unchanged. Not something I can repair with confidence. |
| **Status codes that lie.** Bad login = HTTP 200. Expired session on `__data.json` = HTTP 200. Unknown meter on `__data.json` = HTTP 200 with a nested `{"type":"error","status":404}` node. | various | The client reads bodies, not just status codes. |

## Rate limiting

* The portal returns **`429 {"error":"rate_limited"}` after roughly 120
  requests per minute**, counted across all endpoints for the session/IP.
* No `Retry-After` or rate-limit headers.
* After exhausting the budget it took about 35 seconds to recover.
* I learned this the hard way. My first attempt to pull readings for every
  meter with 8 parallel workers got 102 responses and then 301 × 429.

The client spaces requests evenly (default 100/min) so it doesn't hit the
limit. When a 429 does happen, it retries with exponential backoff
(1, 2, 4, 8, 16 s) before giving up with a 503.

## Things I did not find

* No write endpoints, apart from login and sign-out.
* No way to filter meters by status, make, location or hierarchy on the
  portal side. Search is a substring match on id/serial only, which is why the
  service keeps its own index.
* No bulk readings endpoint. Readings are strictly one meter at a time, which
  under the rate limit means about 4 minutes to fetch all 403 meters.
