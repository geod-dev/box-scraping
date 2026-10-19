# Competitor catalog API: guide for client/frontend developers

This directory is everything needed to build a client (for example a web frontend to
search products and manage the scraping workers) without reading the backend code:

| File | Content |
|---|---|
| `README.md` | this guide: concepts, conventions, workflows, what to show in a UI |
| `openapi.json` | the exact OpenAPI 3.1 schema (every endpoint, parameter and response model); generate a typed client from it, e.g. `npx openapi-typescript docs/api/openapi.json -o src/api/schema.d.ts` |

Interactive docs are served by the API itself: `GET /api/v1/docs` (Swagger UI) and
`/api/v1/redoc`. `openapi.json` is regenerated with `uv run catalog-scraper export-openapi`;
a test fails when the committed file is out of date.

## Base URL, authentication, CORS

* Production: `https://api-haoyfwryqzh3rskh66g9evxy.185.252.233.123.sslip.io/api/v1`
  (Swagger UI: `/api/v1/docs`). Local: `http://localhost:8000/api/v1`
  (`uv run catalog-scraper api`).
* Every endpoint except `GET /health` requires the API key: header `X-API-Key: <key>` (or
  `Authorization: Bearer <key>`). A missing or wrong key gives `401 unauthorized`. The key is
  a server secret (`API_KEY`): a browser frontend should get it from its own backend or from
  a build-time/private setting, never commit it.
* CORS is open (`*`) unless the server sets `CORS_ORIGINS`.

## Conventions

* **JSON everywhere.** Timestamps are ISO 8601 in UTC (`2026-10-19T09:56:26.286370Z`).
* **Decimals are strings** (`"price": "0.5300"`, `"quantity": "1000"`) so prices stay
  exact; parse them with a decimal library, or `Number()` for display only.
* **Lists are paginated**: `?limit=50&offset=0` (limit 1-500) and return
  `{"items": [...], "total": 123, "limit": 50, "offset": 0}`. Exceptions returning plain
  arrays: `GET /competitors`, `GET /categories/tree`, the `/products/{id}/prices*` endpoints.
* **Repeatable filters** are passed several times: `?status=failed&status=not_found`.
* **Errors** always have this shape, with a stable `code` to switch on:

  ```json
  {"error": {"code": "worker_busy", "message": "antalis_fr already has a running catalog_sync job (#12)", "details": {"job_id": 12}}}
  ```

  | HTTP | code | meaning |
  |---|---|---|
  | 401 | `unauthorized` | missing/invalid API key |
  | 404 | `not_found` | unknown resource or route |
  | 409 | `worker_busy` | the competitor already has an active job (`details.job_id`) |
  | 409 | `catalog_required` | price refresh refused: no full catalog sync yet |
  | 409 | `competitor_disabled` / `competitor_not_configured` | competitor cannot run jobs |
  | 409 | `worker_idle` | stop/pause/resume while the competitor has no active job |
  | 409 | `job_not_active` / `job_not_running` / `job_not_paused` / `job_stopping` | invalid job transition |
  | 409 | `job_active` / `run_active` | cannot delete something still running |
  | 409 | `conflict` | uniqueness violation (same product reference/URL, same category URL, same match) |
  | 422 | `validation_error` | invalid body/query (`details` = list of field errors) |
  | 422 | `invalid_options`, `invalid_parent`, `invalid_category`, `invalid_product`, `invalid_match`, `invalid_filter` | semantic validation |
  | 500 | `internal_error` | bug: see server logs |
  | 502/503 | `scrapedo_unavailable` / `scrapedo_not_configured` | `GET /scrapedo/account` only |

## Concepts

### Competitors

A competitor (e.g. `antalis_fr`, "Antalis France") is a website we scrape. Competitors come
from configuration files on the server: they are read-only through the API. A competitor is
addressed by its `key` (or numeric `id`) in URLs. `GET /competitors` returns, for each:

* `last_full_catalog_at` / `last_full_catalog_status`: last time the **whole** catalog was
  scraped (`succeeded`, or `partial` = whole catalog crawled but some pages failed);
* `last_price_refresh_at`: last finished price refresh;
* `stats`: categories, active/missing products, price observations, last price observed;
* `worker`: the competitor's worker (below).

### Workers and jobs

Each competitor has **one worker slot**. A worker executes one **job** at a time; a job is
one **task**:

| task | what it does | allowed when |
|---|---|---|
| `catalog_sync` | crawls every category/listing page, then every product page: creates/updates categories and products (details, characteristics, images) and records current prices; at the end of a complete full sync, products no longer listed are flagged `missing` | no active job |
| `price_refresh` | visits the known product pages and records their current prices only (fast; no category crawl) | no active job **and** a full catalog sync was done (`last_full_catalog_at` set) |

Rules enforced by the server (also by the database):

* **never two jobs on the same competitor**, whatever their tasks (409 `worker_busy`);
* **nothing runs by default**: jobs only start when requested (`POST .../worker/start`).
  (The server can optionally enable an automatic schedule, `AUTO_SCHEDULE`; such jobs have
  `requested_by: "scheduler"`.)
* different competitors can run at the same time.

The job lifecycle (`job.status`):

```
                  start              claimed by the worker (a few seconds)
   (none) ──────────────▶ queued ───────────────────────────▶ running ◀──────┐
                            │ stop                              │  │  pause   │ resume
                            ▼                                   │  ▼          │
                        cancelled                               │ paused ─────┘
                                                                │  │ stop
             ┌───────────────┬───────────────┬─────────────────┬┴──┴──────────────┐
             ▼               ▼               ▼                 ▼                  ▼
         succeeded        partial          failed          cancelled         interrupted
     (all pages OK)  (finished, some   (fatal error,     (stopped by a    (worker restarted
                      pages failed or   e.g. site down,   user)            or redeployed while
                      limited options)  crash, 50 page                     the job ran or was
                                        failures in a row)                 paused)
```

Active statuses: `queued`, `running`, `paused`. Final: the others.

The **worker state** (`GET /competitors/{key}/worker` → `state`) is what a UI should show:

| state | meaning | actions to offer |
|---|---|---|
| `idle` | no active job | start (per `tasks.<task>.allowed`) |
| `queued` | job created, waiting for the worker process | stop |
| `running` | scraping | pause, stop |
| `pausing` | pause requested, finishing the current page (≤ ~15 s) | stop |
| `paused` | holding; keeps its place and the slot | resume, stop |
| `resuming` | resume requested, restarting (≤ ~2 s) | stop |
| `stopping` | stop requested, finishing the current page | (none) |

`tasks` tells which task can be started now and why not:
`{"catalog_sync": {"allowed": true, "reason": null}, "price_refresh": {"allowed": false, "reason": "catalog_required"}}`
(reasons: `worker_busy`, `catalog_required`, `competitor_disabled`).

Notes:

* A pause keeps everything in memory: it does not survive a server restart/redeploy (the
  job then ends `interrupted`). Restarting a `catalog_sync` after an interruption is cheap:
  by default products synced in the last 48 h are not fetched again
  (`options.resume_within_hours`).
* The crawl delay is imposed by the site (Antalis: 10 s between requests), so a full
  Antalis catalog sync takes several hours (~2 700 requests ≈ 7-8 h); a price refresh of
  ~2 200 products ≈ 6 h.
* `GET /health` → `worker.online` says whether the worker process is alive. When it is
  false, jobs can be queued but will not start: show a warning.

### Progress, estimates

A job carries `step`, `step_label`, `step_done`, `step_goal` and a `progress` object:

```json
{
  "percent": 37.4,                       // null while the total is unknown
  "requests_done": 1012,                 // pages fetched so far (unit of work = 1 HTTP request)
  "requests_total": 2706,
  "requests_total_is_estimate": true,    // true until every step's goal is known exactly
  "seconds_per_request": 10.4,
  "seconds_per_request_measured": true,  // false = from history/config, true = measured live
  "elapsed_seconds": 10580,
  "paused_seconds": 120,
  "eta_seconds": 17620,                  // remaining time; null when unknown
  "estimated_finish_at": "2026-10-19T21:12:00Z",  // null while paused
  "errors": 3,                           // failed pages so far
  "steps": [
    {"name": "crawl_listings", "label": "Crawling category/listing pages", "status": "done", "done": 512, "goal": 512, "pending": 0},
    {"name": "sync_products", "label": "Syncing product pages (details + prices)", "status": "running", "done": 500, "goal": 2194, "pending": null},
    {"name": "finalize", "label": "Finalizing", "status": "pending", "done": 0, "goal": null, "pending": null}
  ]
}
```

Steps per task: `catalog_sync` = `crawl_listings` → `sync_products` → `finalize`;
`price_refresh` = `refresh_prices`. While crawling listings the number of listing pages is
not known yet: the total is estimated from the last full catalog sync (or the configured
catalog size) and is at least `done + pending` (pages already discovered).

`counters` holds the raw run counters (`pages_fetched`, `pages_failed`, `categories_seen`,
`products_seen`, `products_created`, `products_updated`, `products_failed`,
`products_without_price`, `observations_created`, `products_marked_missing`,
`credits_used`, ...).

**Before starting**, `POST /competitors/{key}/worker/estimate` (same body as start) returns
`{requests, seconds, credits, listing_pages, products, seconds_per_request, basis[]}` where
`basis` explains each figure in plain text. `GET /competitors/{key}` includes `estimates` for
both tasks with default options. A job also keeps its initial `estimate`.

### Scraped data

* **Categories**: per-competitor hierarchy (`parent_id`, `depth`, `kind`: for Antalis
  `cat`/`sub`/`plp`/`pdp`), `status` `active` | `missing`. `GET /categories/tree?competitor=`
  returns the nested tree with product counts, ideal for a sidebar.
* **Products**: one row per competitor reference (`catalog_reference`; identity
  `identity_key` = `ref:<reference>`). Fields: `title`, `brand`, `description`,
  `long_description`, `attributes` (characteristics as shown on the site:
  `[{"label", "value", ...}]`), `normalized_attributes` (canonical keys, numbers in mm/g/m²:
  `{"length_mm": 305, "color": "Brun", ...}`), `images`, `extra`, `status`
  (`missing` = no longer listed), `url` (competitor page), timestamps.
* **Price observations**: append-only history. One capture (`capture_id`) = all quantity
  tiers read on a page at one time. A tier: `price` (excl. tax for Antalis: `tax_mode`),
  `currency`, quantity band `min_quantity` ≤ q < `max_quantity` (or ≤ when
  `max_inclusive`; null = open), `quantity_unit`, `price_basis` (`unit` = price per
  `price_unit_quantity` × `price_unit`, e.g. per 1 000 sheets), `metadata.displayed`
  (`false` = the site shows "price on request"; value from structured data only).
* **Runs**: one per executed operation (a job has `scrape_run_id`); **run items**: the
  outcome of every page (`succeeded`, `failed`, `not_found`, `no_price`, `skipped`) with
  `error_type`/`error_message`/`http_status`. `GET /run-items?status=failed` lists every
  scraping error.
* **Product matches**: links between equivalent products of *different* competitors
  (`proposed` → `validated`/`rejected`); for future price comparison.

All of them have list/get/create/update/delete endpoints (runs and run items: read and
delete only; they are written by the worker). Deleting a product deletes its price history.
Manual edits may be overwritten by the next catalog sync (the site is the source of truth).

### Notifications

Saved in the database, read/unread. Created by the worker for every job event and error:

| kind | level | when |
|---|---|---|
| `job_started` | info | a job started (message: estimate) |
| `job_paused` / `job_resumed` | info | pause/resume took effect |
| `job_succeeded` | success | finished, everything OK (message: summary, duration, credits) |
| `job_partial` | warning | finished, some pages failed or options limited the scope (`data.error_types`, `data.incomplete_reasons`) |
| `job_failed` | error | fatal error, or stopped by the safety guard (50 consecutive page failures) |
| `job_cancelled` | info | stopped by a user |
| `job_interrupted` | warning | the worker restarted while the job was running/paused |
| `job_crashed` | error | unexpected exception (`data.traceback`), followed by `job_failed` |
| `error_burst` | error | 10 pages failed in a row (site blocking, proxy outage...) |
| `page_errors` | error | digest of new page errors during a run, at most every 15 min (`data.errors`: url, status, error_type, message) |

Each notification: `id`, `created_at`, `level`, `kind`, `title`, `message`,
`competitor_id`, `job_id`, `scrape_run_id`, `data`, `read_at`, `read`. Endpoints: list
(`?unread=true`, `level`, `kind`, `competitor`, `job_id`, `since_id`), `unread-count`
(`{"unread": 3, "by_level": {"error": 1, "success": 2}}`), `POST /{id}/read`,
`POST /{id}/unread`, `POST /read-all` (optionally `competitor`, `level`, `up_to_id`),
`DELETE /{id}`.

## Typical client flows

**Dashboard / worker panel**

1. `GET /health` (worker online?), `GET /competitors` (cards with state, last full catalog,
   stats), `GET /notifications/unread-count` (bell badge).
2. Start: `POST /competitors/antalis_fr/worker/start` with `{"task": "catalog_sync"}` or
   `{"task": "price_refresh"}` → `202` + the job (`status: "queued"`). Disable buttons from
   `worker.tasks`; still handle 409.
3. While `state` ≠ `idle`, poll `GET /competitors/antalis_fr/worker` every 2-5 s: progress
   bar from `job.progress.percent` (indeterminate when null), `step_label`,
   `requests_done/requests_total`, ETA, `errors`; the previous result is in `last_job`.
4. `POST .../worker/pause`, `.../worker/resume`, `.../worker/stop` (all `202` + job).
5. History: `GET /jobs?competitor=antalis_fr` (newest first); details of what was scraped:
   `GET /runs/{job.scrape_run_id}` and `/runs/{id}/items?status=failed`.

**Notifications**: poll `GET /notifications?since_id=<last id seen>` (or `unread-count`)
every 15-30 s; mark read on display.

**Product search**

* `GET /products?competitor=antalis_fr&q=caisse carton&category_id=12&attr=length_mm:305&brand=Siat&status=active&has_price=true&sort=title&order=asc&limit=50`
  - `q`: every word must appear in title, references, brand or description (case-insensitive);
  - `category_id` includes sub-categories (`include_subcategories=false` to disable);
  - `attr=key:value` matches `normalized_attributes` exactly (JSON value: `305`, `"Brun"`
    or plain text), repeatable (AND);
  - `sort`: `title`, `reference`, `brand`, `created_at`, `updated_at`, `last_seen_at`,
    `last_price_observed_at`, `id`.
  Each item has `latest_price`: `{from_price, min_price, tiers, currency, tax_mode,
  price_unit, price_unit_quantity, quantity_unit, observed_at, displayed}` or null.
* `GET /products/facets` (same filters) → brands, statuses and the most frequent values of
  each normalized attribute, with counts: build filter widgets from it.
* `GET /categories/tree?competitor=antalis_fr` for the category navigation.
* Product page: `GET /products/{id}` (adds `category_path`, all `latest_prices` tiers,
  `attributes`, `images`, `long_description`), `GET /products/{id}/prices/history` (chart;
  `?quantity=` to follow one tier), `GET /products/{id}/prices/for-quantity?quantity=500`
  (price applicable to a quantity). `GET /products/by-reference/antalis_fr/503623` finds a
  product by reference.

## Endpoint index

All paths are relative to `/api/v1`. Request/response details: `openapi.json`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | API, database and worker liveness (no auth) |
| GET | `/stats` | global counters |
| GET | `/scrapedo/account` | Scrape.do plan and remaining credits (max 10 calls/min) |
| GET | `/competitors` | competitors with stats and worker state |
| GET | `/competitors/{key}` | + config snapshot and task estimates |
| GET | `/competitors/{key}/worker` | worker state, active job with progress, last job, allowed tasks |
| POST | `/competitors/{key}/worker/start` | start a task (`{"task", "options"}`) |
| POST | `/competitors/{key}/worker/pause` | pause the running job |
| POST | `/competitors/{key}/worker/resume` | resume the paused job |
| POST | `/competitors/{key}/worker/stop` | cancel the active job |
| POST | `/competitors/{key}/worker/estimate` | estimate a task without starting it |
| GET | `/jobs` | job history (`competitor`, `task`, `status`, `active`) |
| GET | `/jobs/{id}` | one job |
| POST | `/jobs/{id}/cancel`, `/jobs/{id}/pause`, `/jobs/{id}/resume` | same actions by job id |
| DELETE | `/jobs/{id}` | delete a finished job |
| GET/POST | `/categories` | list/search (`competitor`, `parent_id`, `roots`, `status`, `kind`, `q`) / create |
| GET | `/categories/tree` | nested tree (`competitor` required) |
| GET/PATCH/DELETE | `/categories/{id}` | one category (with `path`, counts) / update / delete |
| GET/POST | `/products` | search / create |
| GET | `/products/facets` | filter values with counts |
| GET | `/products/by-reference/{competitor}/{reference}` | lookup by reference |
| GET/PATCH/DELETE | `/products/{id}` | detail / update / delete (with its prices) |
| GET | `/products/{id}/prices` | latest capture (all tiers) |
| GET | `/products/{id}/prices/history` | all observations, oldest first |
| GET | `/products/{id}/prices/for-quantity` | applicable latest tier for `quantity` |
| GET/POST | `/observations` | list (`product_id`, `competitor`, `run_id`, `capture_id`, `since`, `until`) / add a manual price |
| GET/PATCH/DELETE | `/observations/{id}` | one observation |
| GET | `/runs` | runs (`competitor`, `operation`, `status`, `full_scope`) |
| GET/DELETE | `/runs/{id}` | run detail with `item_counts` and `stats` / delete |
| GET | `/runs/{id}/items` | page outcomes of a run (`status`, `item_type`) |
| GET | `/run-items` | page outcomes across runs (`competitor`, `status`, `item_type`, `product_id`, `url`) |
| GET/POST | `/product-matches` | list (`product_id`, `status`) / create |
| GET/PATCH/DELETE | `/product-matches/{id}` | one match |
| GET | `/notifications` | list (`unread`, `level`, `kind`, `competitor`, `job_id`, `since_id`) |
| GET | `/notifications/unread-count` | unread badge |
| POST | `/notifications/read-all` | mark all (filtered) as read |
| GET/DELETE | `/notifications/{id}` | one notification |
| POST | `/notifications/{id}/read`, `/notifications/{id}/unread` | toggle read state |

## Start options (body of `worker/start` and `worker/estimate`)

`catalog_sync` (all optional; leave empty for the normal full sync):

| option | default | effect |
|---|---|---|
| `resume_within_hours` | 48 | products synced less than N h ago are confirmed without refetch (0 = refetch all) |
| `root_urls` | config | crawl only from these URLs (partial scope) |
| `max_listing_pages` / `max_products` | none | stop after N pages/products (partial scope, for tests) |
| `skip_products` | false | only crawl categories and product links |
| `allow_mass_missing` / `max_missing_ratio` | false / 0.2 | safety threshold for flagging products missing |

A catalog sync with partial-scope options never updates `last_full_catalog_at` and never
flags anything missing.

`price_refresh` (all optional; empty = every active product):

| option | effect |
|---|---|
| `category` | category id, external id or URL: whole subtree |
| `references` | list of catalog references |
| `retry_failed_run_id` | only the products that failed in that run |
| `skip_fresh_hours` | skip products priced less than N h ago |
| `include_missing` | also refresh products flagged missing |
| `update_details` | also update descriptions/characteristics/images |
| `limit` | at most N products |
