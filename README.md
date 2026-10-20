# Competitor catalog scraper

Scrapes competitor e-shops (**Antalis France**, packaging sector, from its HTML; **Packdiscount
France**, from its JSON API) through the **Scrape.do** API and stores categories, products,
characteristics, images and an
append-only price history in **PostgreSQL**.

Two independent operations:

| | command | what it does |
|---|---|---|
| **A** | `catalog-scraper catalog-sync` | crawl the category tree from the configured roots, discover product URLs, store categories/products **and** their current prices |
| **B** | `catalog-scraper price-refresh` | revisit already known product URLs and append price observations only |

On the server they run as **worker jobs** driven by an **HTTP API** (`catalog-scraper api`
+ `catalog-scraper worker`): start/pause/resume/stop a job per competitor, live progress
and ETA, CRUD on the scraped data, persisted notifications. Nothing scrapes unless a job
is requested. API guide for client/frontend developers: [`docs/api/`](docs/api/README.md). The API is also
an MCP server (`/api/v1/mcp?api_key=<key>`) exposing every endpoint as a tool.

A third job, **product matching** (§5), associates every product of the other competitors
with the corresponding **Antalis** product (the reference catalog): Voyage AI embeddings +
lexical search + dimension comparison, automatic association when unambiguous, human review
otherwise, incremental reruns and obsolescence tracking when products change.

Site findings (what is confirmed vs assumed) are in
[`docs/antalis-observations.md`](docs/antalis-observations.md) and
[`docs/packdiscount-observations.md`](docs/packdiscount-observations.md).

---

## 1. Quick start

Requirements: Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/), Docker (for PostgreSQL).

```bash
cp .env.example .env            # then set SCRAPEDO_API_KEY (never commit .env)
docker compose up -d            # PostgreSQL 16 on localhost:5433 (+ a test database)
uv sync                         # install dependencies
uv run catalog-scraper db-upgrade        # apply migrations (= alembic upgrade head)
uv run catalog-scraper scrapedo-check    # account info + 1 request (1 credit)
```

Then:

```bash
# Operation A - full catalog discovery of the configured root (packaging sector).
uv run catalog-scraper catalog-sync --competitor antalis_fr

# Operation B - price-only refresh of every known active product
uv run catalog-scraper price-refresh --competitor antalis_fr
```

### Environment variables

| variable | required | meaning |
|---|---|---|
| `SCRAPEDO_API_KEY` | yes (network commands) | Scrape.do token. Read from the environment only, never logged (see §6). |
| `VOYAGE_API_KEY` | matching | Voyage AI key (embeddings). Without it matching is disabled (`matching_not_configured`). Never logged. |
| `MATCHING_CONFIG` | no | defaults to `config/matching.yaml` (weights, thresholds, taxonomy, Voyage rate limits) |
| `DATABASE_URL` | yes | SQLAlchemy URL, e.g. `postgresql+psycopg://scraper:scraper@localhost:5433/competitor_catalog` |
| `TEST_DATABASE_URL` | tests | database whose schema the tests drop/recreate (default: `competitor_catalog_test` on the compose server) |
| `LOG_LEVEL` | no | `INFO` by default |
| `COMPETITOR_CONFIG_DIR` | no | defaults to `config/competitors` |
| `HTML_ARCHIVE_DIR` | no | if set, every fetched page is saved there (debugging, new fixtures) |
| `API_KEY` | API | key clients send as `X-API-Key` (`API_AUTH_DISABLED=true`: no auth, local dev only) |
| `CORS_ORIGINS`, `API_HOST`, `API_PORT` | no | API: allowed origins (default `*`), bind address (`0.0.0.0:8000`) |
| `AUTO_SCHEDULE` | no | worker: `true` = also enqueue due jobs automatically (default `false`: only API requests) |
| `WORKER_MAX_CONCURRENT_JOBS`, `WORKER_POLL_SECONDS`, `STALE_JOB_MINUTES` | no | worker tuning (4, 3, 30) |
| `WORKER_AUTO_RESUME` | no | worker: restart the jobs interrupted by a worker restart/redeploy at start-up (default `true`) |

`.env` is loaded automatically (real environment variables take precedence) and is
git-ignored; `.env.example` documents the variables without values.

### Database setup and migrations

* `docker compose up -d` starts PostgreSQL 16 (`scraper`/`scraper`, port **5433**) and
  creates both `competitor_catalog` and `competitor_catalog_test`
  (`docker/initdb/01-test-db.sql`). Any PostgreSQL ≥ 13 works: just point `DATABASE_URL` to it.
* Migrations are Alembic scripts in `migrations/versions/`:
  `uv run catalog-scraper db-upgrade` or `uv run alembic upgrade head`
  (`uv run alembic downgrade base` to drop everything). New migration after a model
  change: `uv run alembic revision --autogenerate -m "..."`, then review it.

---

## 2. Commands

All commands: `uv run catalog-scraper <command> --help`.

### Operation A - `catalog-sync`

```bash
uv run catalog-scraper catalog-sync --competitor antalis_fr                       # full scope
uv run catalog-scraper catalog-sync --competitor antalis_fr --skip-products       # tree + product URLs only
uv run catalog-scraper catalog-sync --competitor antalis_fr --root-url <category or family URL>   # subtree
uv run catalog-scraper catalog-sync --competitor antalis_fr --max-products 50     # sample
```

* Breadth-first crawl from `root_urls` of the competitor config. Every fetched page is
  classified from its own content (not from its URL), its category is upserted with its
  parent taken from the page's navigation breadcrumb, children / product links / next page
  are queued; pagination pages are attached to the category of their first page.
* Each product page is parsed (reference, title, descriptions, full characteristics,
  images, category) and **its prices are recorded with the same function as
  `price-refresh`** (`repositories.record_price_snapshot`).
* **Idempotent**: categories are matched by real external id then canonical URL, products
  by `(competitor, identity_key)`; existing rows are updated, new ones inserted, nothing
  is duplicated (DB unique constraints enforce it). Price observations are appended.
* Scope: only categories whose own breadcrumb contains a root are stored/followed, so the
  crawl stays inside `packaging-machines-emballage`.
* `--root-url`, `--max-products`, `--max-listing-pages`, `--skip-products` make the run
  *partial scope*: such runs never flag anything as missing. Product URLs discovered but
  not synced (`--skip-products`, `--max-products`) are still recorded as `skipped` run items,
  and `scrape_runs.stats` holds `product_links_discovered` / `duplicate_product_links_ignored`.
* Robustness: every page/product is processed in isolation — a fetch error, block page,
  parse error or unexpected exception is recorded on the run item, makes the run `partial`,
  and the crawl continues. Only a fatal Scrape.do error (401: credits/token) stops the run.
  A listing that 404s, a category page without breadcrumb (scope undecidable) or a family
  whose announced item count is not reached also make the run `partial`.
* Designed to be re-run periodically (e.g. every 6 months, from cron / a scheduler of
  your choice; none is bundled) to pick up new products and flag removed ones.

### Operation B - `price-refresh`

```bash
uv run catalog-scraper price-refresh --competitor antalis_fr                         # all active products
uv run catalog-scraper price-refresh --competitor antalis_fr --category HQ02947      # subtree (id, external id or URL)
uv run catalog-scraper price-refresh --competitor antalis_fr --product 503623 --product 586283
uv run catalog-scraper price-refresh --competitor antalis_fr --retry-failed 12       # only failures of run #12
uv run catalog-scraper price-refresh --competitor antalis_fr --skip-fresh-hours 24   # resume after a crash
uv run catalog-scraper price-refresh --competitor antalis_fr --no-update-details     # prices only
```

* Loads products from the database (never crawls categories), fetches their stored detail
  URL, records the prices and appends observations linked to the run.
* The fetched page is the full product page: descriptions / characteristics / images are
  re-parsed from it too (no extra request), unless `--no-update-details`. Use it to apply an
  adapter or attribute mapping change without a full catalog sync. A details parse error
  only adds a warning; the prices are still recorded.
* If the page now shows another reference (redirect) — or, for URL-identity products,
  another canonical URL — the item fails and no price is stored.
* `--retry-failed RUN_ID` accepts a price-refresh or a catalog run (failed items are matched
  by product id, or by URL when the product row could not be written).
* If a price table is present but cannot be fully parsed, the whole capture is rejected
  (item `failed`): no partial set of tiers and no fallback to the headline price is stored.

### Inspection

```bash
uv run catalog-scraper runs --competitor antalis_fr           # recent runs with counters and credits
uv run catalog-scraper runs --run 3                           # one run + failed items
uv run catalog-scraper categories --competitor antalis_fr     # stored tree with product counts
uv run catalog-scraper product --competitor antalis_fr --product 503623
uv run catalog-scraper prices  --competitor antalis_fr --product 586283                # latest tiers
uv run catalog-scraper prices  --competitor antalis_fr --product 586283 --quantity 150 # tier for a quantity
uv run catalog-scraper prices  --competitor antalis_fr --product 586283 --history      # full history
uv run catalog-scraper fetch --competitor antalis_fr <url> --out page.html             # debug / fixtures
```

Exit code: `0` = run succeeded, `2` = run partial/failed (details in `scrape_runs`).

### API and worker

```bash
uv run catalog-scraper api       # migrations, then the HTTP API on :8000 (docs: /api/v1/docs)
uv run catalog-scraper worker    # migrations, then executes the jobs requested through the API
uv run catalog-scraper export-openapi   # regenerate docs/api/openapi.json after an API change
uv run catalog-scraper match --competitor packdiscount_fr [--mode incremental|deprecated|all]
```

* A **job** = one task (`catalog_sync` = operation A, `price_refresh` = operation B,
  `matching` = §5) for one competitor. The database guarantees **at most one active job per competitor** (partial
  unique index on `worker_jobs`); a price refresh is refused until a full catalog sync
  finished (`competitors.last_full_catalog_at`).
* The worker process claims queued jobs (`SKIP LOCKED`), runs each in its own thread with
  its own fetcher/rate limiter (competitors in parallel, never twice the same site), writes
  progress (step, done/goal, %, ETA from measured speed) and reads cancel/pause requests
  at every page. A pause holds the thread between two pages (state kept in memory).
* Estimates (`services/estimates.py`) count requests from the last full catalog sync / the
  database / `estimate_hints` of the competitor config, and time per request from the
  durations of previous runs (otherwise the longer of the crawl delay and a typical request).
* Notifications (`notifications` table) are written for each job event and for errors
  (bursts of failures, periodic digests of page errors, crashes).
* **Retrying failed pages**: `POST /runs/{id}/retry-failed` queues a job that refetches only
  the pages that failed in that run — for a catalog run, a `catalog_sync` with
  `retry_failed_run_id` (its failed product pages, including products never created, and
  its failed listing pages with their sub-pages; never a full sync, never flags anything
  missing); for a price refresh run, a `price_refresh` with `retry_failed_run_id`.
* **Restarting a stopped job**: `POST /jobs/{id}/restart` (interrupted, failed or cancelled
  job) queues the same task, linked by `resumed_from_job_id`, skipping the work already
  done: products synced (catalog sync, `resume_within_hours`) or priced (price refresh,
  `skip_fresh_hours`) since the stopped job started; matching is incremental anyway. The
  category pages a catalog sync already fetched are **replayed from the database** (table
  `listing_page_cache`, compressed, kept until a full sync completes, purged after 7 days):
  no request, no credit, and the crawl, completeness checks and missing-flagging work as
  for a fresh crawl. Only the remaining category pages and products are fetched.
* **Automatic resume after a redeploy**: at start-up the worker restarts every job
  interrupted (by its own shutdown or a dead worker) in the last 24 h when nothing else was
  started on that competitor since (`requested_by: "auto_resume"`, notification
  `job_auto_resumed`). After 3 automatic restarts in a row of the same work it stops and
  notifies `job_auto_resume_stopped` (crash-loop guard). `WORKER_AUTO_RESUME=false` disables it.
* CLI operations (`catalog-sync`, `price-refresh`, `match`) still work directly; they do not go
  through the job queue, so do not run one on a competitor whose worker is busy.

---

## 3. Architecture

```
src/competitor_catalog/
  http/            generic transport: Fetcher protocol, Scrape.do client, rate limiter, errors
  core/            engine-facing contract and shared utilities
    adapter.py       CompetitorAdapter base class + dynamic loading from config
    types.py         CategoryData, ListingPage, ProductData, PriceSnapshot, PriceTier...
    config.py        competitor YAML config loader/validation
    normalize.py     numbers (FR/EN), units, characteristic structuring, normalized attributes
    html.py, urls.py lxml helpers (JSON-LD, canonical...), URL normalization
  adapters/antalis/ Antalis-specific parsing only (no I/O, no DB)
  db/              SQLAlchemy models, session helpers, repositories (no HTML knowledge)
  services/        orchestration: catalog_sync.py (A), price_refresh.py (B), runs.py (tracking,
                   progress/stop hooks), jobs.py (job rules, execution, progress), dispatcher.py
                   (worker process), estimates.py, notifications.py, worker.py (opt-in schedule)
  matching/        product matching (§5): config.py, text.py (fields read, hashes), dimensions.py,
                   features.py (types/characteristics), lexical.py, embeddings.py (Voyage + cache),
                   engine.py (pure scoring/decision), service.py (runs, change detection, decisions)
  api/             FastAPI app: main.py (factory, errors, auth), schemas.py, views.py, routes/
  app.py, cli.py   wiring and command line
docs/api/                            API guide + openapi.json for client developers
config/competitors/antalis_fr.yaml   competitor configuration (data, no code)
config/matching.yaml                 matching: reference catalog, weights, thresholds, taxonomy
migrations/                          Alembic migrations
tests/                               fixtures + unit, DB and live tests
```

Data flow: `service -> Fetcher.fetch(url) -> adapter.classify/parse_* -> core types -> repositories -> PostgreSQL`.

* The **engine** (services + repositories + HTTP client) knows nothing about HTML, CSS
  selectors or Antalis. It only manipulates the dataclasses of `core/types.py`.
* An **adapter** knows how to read one website. It does no I/O and no persistence, which
  makes it testable on saved HTML.
* The **competitor config** (YAML) holds everything that is data: base URL, root URLs,
  crawl delay, retries, Scrape.do options (`render`, `super`, `geo_code`, `timeout_ms`),
  and adapter options such as the characteristic label → normalized key mapping.

### Adding a competitor

1. Write `src/competitor_catalog/adapters/<name>/adapter.py` with a subclass of
   `CompetitorAdapter` implementing four methods:
   * `classify(page) -> PageKind.LISTING | PageKind.PRODUCT` (raise `UnexpectedPageError`
     for block/error pages — they must never look like an empty category);
   * `parse_listing(page) -> ListingPage` (category info + ancestors, child URLs,
     product links, next page, announced item count);
   * `parse_product(page) -> ProductData` (details + `PriceSnapshot`);
   * `parse_prices(page) -> ProductPricePage` (reference + `PriceSnapshot`, usually
     reusing the same private price parser).
   Reuse `core.html` (lxml helpers, JSON-LD) and `core.normalize` (decimal parsing, units,
   `build_attribute`, `build_normalized_attributes`).
2. Add `config/competitors/<key>.yaml` (`key`, `name`, `base_url`, `adapter:
   "module:Class"`, `root_urls`, `request_interval_seconds`, `scrapedo`, `options`).
3. Save a few pages with `catalog-scraper fetch ... --out tests/fixtures/<name>/...` and
   write parser tests like `tests/test_antalis_parsers.py`.
4. Run `catalog-scraper catalog-sync --competitor <key> --max-products 20`.

No change to the engine, CLI or database is needed: the competitor row is created from the
config on first use. `tests/toy_adapter.py` is a complete (JSON-based) second competitor
used by the engine tests and shows the minimum required.

Scope note: a generic "visual" parser was deliberately not attempted; adapters are small
explicit classes and everything reusable lives in `core/`.

---

## 4. Database model

Tables (see `src/competitor_catalog/db/models.py` and `migrations/versions/`; a test checks
that the migrations build exactly the models):

### `competitors`
`id`, `key` (unique, = config key), `name`, `base_url`, `adapter` (import path),
`config` (JSONB snapshot of the YAML used), `enabled`, timestamps.

### `categories` (competitor-specific hierarchy)
`id`, `competitor_id`, `parent_id` (self FK, nullable), `name` (navigation label), `slug`
(last URL segment, informational), `url` (canonical), `external_id` (only a real id
exposed by the site: Antalis `pageDataModel.category_type_id`, e.g. `HQ02953`; `NULL` for
the root whose value is the placeholder `HQ00000`), plus:

* `kind` – site node type (`sector`, `sub_category`, `product_listing`, `product_family`):
  Antalis families are SKU containers, so the type is needed to tell them from browsing nodes.
* `depth` – number of navigation ancestors.
* `breadcrumb` (JSONB) – navigation path as observed; `details` (JSONB) – page title, SEO
  title, announced item count, parent URL.
* lifecycle: `status` (`active`/`missing`), `first_seen_at`, `last_seen_at`,
  `last_seen_run_id`, `missing_since`.

Uniqueness: `UNIQUE(competitor_id, url)` and a partial unique index on
`(competitor_id, external_id) WHERE external_id IS NOT NULL`. URL slugs are never used as ids.

### `products` (one reference in one competitor catalog)
`id`, `competitor_id`, `category_id` (most specific known category = Antalis product
family), `catalog_reference` (reference in its own catalog),
`external_id`, `url` (exact canonical product detail URL, independent from category URLs),
`title`, `description`, `attributes`, `images`, timestamps and lifecycle (`status`,
`first_seen_at`, `last_seen_at`, `last_seen_run_id`, `missing_since`). Added fields:

* `identity_key` – uniqueness key `UNIQUE(competitor_id, identity_key)`:
  `ref:<catalog_reference>` normally; **fallback** `url:<canonical url>` for sites that
  expose no stable reference (if a reference appears later the row is upgraded in place).
* `long_description` – the marketing/technical text of the product tab, kept apart from
  the short commercial description (`description`).
* `brand` – JSON-LD brand (useful for matching).
* `normalized_attributes` (JSONB, GIN index) – canonical comparable fields derived from
  the characteristics: `length_mm`, `width_mm`, `height_mm`, `inner_*_mm`, `thickness_um`,
  `roll_length_m`, `grammage_g_m2`, `material`, `flute_type`, `flute_wall`, `color`,
  `packaging_unit`, `pallet_quantity`... Needed for filtering and cross-competitor
  matching without knowing each site's labels. Mapping is configuration (YAML).
* `extra` (JSONB) – competitor-specific details: sales units/conditioning, price unit,
  documents (technical sheets), weight, availability, breadcrumb, category reference.
* `details_updated_at`, `last_price_observed_at`.

`attributes` (JSONB array) keeps **every** characteristic with its original label/value:

```json
{"group": "Dimensions", "label": "Longueur (mm)", "value": "305", "key": "longueur_mm",
 "name": "Longueur", "unit": "mm", "numeric_value": 305, "source": "characteristics"}
```

(`source` is `characteristics`, `technical_information` or `pictogram`; an attribute shown
without value has `"empty": true`.) `images` (JSONB array): `{"url", "position", "alt",
"variants": {"LOSQS": url, "LOSQL": url}}` — only URLs present on the page.

**Missing vs empty**: `NULL` = not observed (section absent / not parsed); `[]`, `{}` or
`""` = the site confirmed it is empty. Updates never replace a known value by `NULL`.

### `scrape_runs`, `scrape_run_items`
A run = one execution of operation A or B: `operation`, `status`
(`running`, `succeeded`, `partial`, `failed`, `aborted`), `started_at`, `finished_at`,
`heartbeat_at`, `full_scope`, `params`, counters (pages fetched/failed, categories, products
seen/created/updated/failed/without price, observations, marked missing, Scrape.do
credits), `stats` (incomplete reasons, count mismatches), `error`.
`scrape_run_items` stores the outcome of every page/product of a run
(`succeeded`, `failed`, `not_found`, `no_price`, `skipped`, error type/message, HTTP status,
parser warnings): it is what distinguishes a failed scrape from a page without price, and
what `--retry-failed` uses.

**Missing products**: only a run that is `succeeded` (no failed page, no pagination
mismatch) **and** full scope may set `status='missing'` on categories/products it did not
see. Safety net: if more than 20 % of active products would be flagged, nothing is flagged
unless `--allow-mass-missing` is given. A product seen again becomes `active` again.

### `scrape_observations` (append-only price history)
One row = one price observed for one product and one quantity tier at one moment.

| column | meaning |
|---|---|
| `product_id`, `scrape_run_id`, `observed_at` (timestamptz) | what, which run, when |
| `capture_id` (uuid) | all tiers read from the same page at the same time |
| `quantity` | exact tier quantity when the site gives one |
| `min_quantity`, `max_quantity`, `max_inclusive` | band: `min` inclusive (NULL = no lower bound); `max` NULL = **open-ended**; `max` exclusive unless `max_inclusive` |
| `quantity_unit` | unit of the quantities (Antalis: `unité`) |
| `price` `NUMERIC` (unbounded), `currency` | exact decimal, never floating point, never rounded |
| `price_basis` | `unit` (price of one `price_unit`), `total` (price of the whole tier), `unknown` |
| `price_unit`, `price_unit_quantity` | the price is for `price_unit_quantity` × `price_unit`: `1 unité`, or `1000 feuille(s)` when the site says "€ 101,47 / 1 000 feuille(s)" |
| `tax_mode` | `excl_tax`, `incl_tax`, `unknown` (Antalis shows "Prix H.T." → `excl_tax`) |
| `is_promotional` | true / false / NULL (unknown) |
| `metadata` (JSONB) | competitor-specific: strikethrough price, discount, tier label ("3 boite(s)"), raw `data-variant`, availability... |

No uniqueness on (product, quantity): the same tier is observed again at every run.
Only observed prices are stored: no tier is interpolated, a page without price creates no
row (its run item is `no_price`), a failed fetch creates no row (run item `failed`).

Antalis example (SKU 586283): `[36,108) 3.07`, `[108,180) 2.76`, `[180,∞) 2.29` EUR HT per
unit. A single-price product (503623) gets one band `[10,∞)` where 10 is the minimum
order quantity shown in the page data.

**Latest valid price**: view `latest_price_observations` = all tiers of the most recent
capture of each product. Because only successful captures exist, a later failure never
hides the last known price; whether it is stale is visible from `observed_at` and from the
product's latest run items.

```sql
-- latest tiers of a product
SELECT * FROM latest_price_observations WHERE product_id = 42;
-- applicable tier for a quantity (in quantity_unit)
SELECT * FROM price_for_quantity(42, 150);
-- history of the tier covering quantity 150
SELECT observed_at, price FROM scrape_observations
WHERE product_id = 42 AND (min_quantity IS NULL OR min_quantity <= 150)
  AND (max_quantity IS NULL OR 150 < max_quantity OR (max_inclusive AND max_quantity = 150))
ORDER BY observed_at;
-- did the last price refresh fail for this product, or show no price?
SELECT r.id, i.status, i.error_type, i.error_message FROM scrape_run_items i
JOIN scrape_runs r ON r.id = i.scrape_run_id WHERE i.product_id = 42 ORDER BY i.id DESC LIMIT 1;
```

### Product matching (§5)
Product rows are **never merged**: each competitor keeps its own record; a match links a
competitor product to an Antalis product.

* `product_matches` – the **current** state, **exactly one row per competitor product**
  (`UNIQUE(product_id)`: reruns update it, they cannot duplicate it). `status`
  (`auto_matched`, `validated`, `needs_review`, `no_match`, `rejected`, `deprecated`),
  `antalis_product_id` (the association: set for `auto_matched`/`validated`),
  `proposed_antalis_product_id` (the engine's best candidate), `score` / `runner_up_score` /
  `score_gap` (0-100, not a probability), `dimensions_compared`, `decision_reasons` (JSONB
  codes + messages); change detection: `product_hash`, `antalis_product_hash`,
  `reference_catalog_hash`, `config_hash`, `computed_at`, `job_id`; human decision:
  `decided_by`, `decided_at`, `decision_comment`, `manual_choice` (another product than the
  proposal), `rejected_antalis_product_ids` (never proposed again); obsolescence:
  `deprecated_at`, `deprecated_reason`, `previous_status` (the association stays in place,
  visible); `revision` (optimistic lock: a run never overwrites a decision taken meanwhile).
* `product_match_candidates` – the ranked candidates of the latest computation (10 best +
  5 best excluded) with `score`, `semantic_score`, `lexical_score`, `dimension_score` and
  `details` (JSONB: weights, raw cosines, shared terms, compared measures and
  characteristics, differences, exclusions, review flags, retrieval signals).
* `product_match_events` – **append-only history**: `computed`, `recomputed` (only when the
  result changed or after a deprecation), `validated`, `rejected`, `deprecated`, `reset`,
  with from/to status and product, score, actor (`engine` or the reviewer), reason and a
  JSONB snapshot of the previous state (top candidates included). Product ids are plain
  columns so the history survives the deletion of an Antalis product.
* `text_embeddings` – the global embedding cache: one float32 vector per embedded text
  (`text_hash` of model + dimensions + input type + text, the text, `tokens` billed), shared
  by every product of every competitor that has this text.
* `product_embeddings` – the text each product was last embedded with (`text_hash`, `tokens`
  billed when its text was new). Its `embedding` column is deprecated (vectors moved to
  `text_embeddings` by migration 0006, no longer written).

```sql
-- competitor prices next to the Antalis product they are associated with
SELECT a.catalog_reference AS antalis_ref, a.title, c.key, p.catalog_reference, p.title, m.status, m.score
FROM product_matches m JOIN products p ON p.id = m.product_id JOIN competitors c ON c.id = m.competitor_id
JOIN products a ON a.id = m.antalis_product_id
WHERE m.status IN ('auto_matched', 'validated') ORDER BY a.catalog_reference;
-- evaluation: engine score vs human decisions (to tune the thresholds)
SELECT e.event, width_bucket(e.score, 50, 100, 10) AS bucket, count(*) FROM product_match_events e
WHERE e.event IN ('validated', 'rejected') GROUP BY 1, 2 ORDER BY 2, 1;
```

---

## 5. Product matching

Antalis (`reference_competitor` in `config/matching.yaml`) is the **reference catalog**: every
product of the *other* competitors is associated with the Antalis product that corresponds
to it best. Antalis products are never matched themselves.

**Prerequisites** (otherwise the job is refused with the code in brackets): a successful full
catalog sync of the competitor (`catalog_required`) and of Antalis
(`reference_catalog_required`), i.e. both catalogs parsed; `VOYAGE_API_KEY` set
(`matching_not_configured`). Only active products whose details were parsed are matched.

**Running** – a worker job `matching` (`POST /api/v1/matching/competitors/{key}/run`, or the
generic worker endpoints), followed like any job; or `catalog-scraper match`. Modes:

| mode | products (re)computed |
|---|---|
| `incremental` (default) | never matched + `deprecated` + engine results computed with another config or another state of the Antalis catalog |
| `deprecated` | `deprecated` only (`POST /api/v1/matching/deprecated/rerun` does it for every competitor) |
| `all` | every engine result (after a threshold change, to re-check everything) |

Human decisions (`validated`, `rejected`) are **never recomputed** unless they became
`deprecated`. At the end of a run, a `matching_summary` notification gives the number of
products needing a human review.

**Engine** (`matching/engine.py`, pure; `text.py`, `dimensions.py`, `features.py`, `lexical.py`):

1. *What is read*: the title + the category name (which carries the product family when the
   title is terse) are the compared texts (embedding and TF-IDF); the description is read
   for characteristics only (color, flute wall...), and the normalized attributes for
   dimensions. Descriptions are kept out of the compared texts
   (`text.description_in_similarity: false` in `config/matching.yaml`): Antalis writes a short
   spec line, Packdiscount long SEO/FAQ prose that drowned the title (identical titles scored
   0.18 lexical / 0.855 semantic with it, 1.0 / 0.968 without). Packdiscount placeholder
   descriptions ("➤ Voir toutes les références") are ignored through
   `matching.ignore_description_patterns` of the competitor YAML (the long description is
   used instead when present).
2. *Signals*: **semantic** = cosine of `voyage-4` embeddings (1024 dims, rescaled from
   `[0.55, 0.95]` to 0..1: different product families are ~0.5-0.7, the same family with
   another construction ~0.87, the same product ~0.96); **lexical** = TF-IDF cosine of the words (numbers excluded,
   IDF learnt on the Antalis catalog, rescaled with ceiling 0.6); **dimensions** = measures
   with an explicit unit, from the normalized attributes first, else the title
   (`L x l x h` box, roll width x length, flat format, thickness µm, grammage, volume):
   a measure matches when every value is the same within `dimensions.tolerance` (0.5 %:
   rounding only, 1 mm on 230 mm) and scores 1, or 0.9 when the values only match in another
   order (215 x 305 vs 305 x 215). Any other difference is **another product** (800 x 600 x
   600 is not 780 x 580 x 600): the measure scores 0 and the pair scores its worst measure.
   Only reliable measures are kept (units explicit), and two products are *comparable* when
   they share at least one measure.
3. *Retrieval*: the union of the 25 best candidates by embedding, 25 by lexical similarity
   and 15 with the same dimensions (closest texts first) is re-ranked (a candidate missed by
   one signal is caught by another).
4. *Exclusions* (certain incompatibilities, never associated): product types known on both
   sides and not compatible (`product_types` + `compatible_types` of the config: a box is
   never a tape, a tape dispenser is not a tape...), `exclude` characteristics in conflict
   (single vs double flute), a shared measure that differs (`dimension_mismatch`;
   `dimensions.mismatch_policy: review` would only block the automatic association),
   products rejected by a reviewer for this product.
5. *Score* = `100 x Σ weight x sub-score`, weights `semantic 0.50 / lexical 0.20 /
   dimensions 0.30` when the dimensions are comparable, else `0.75 / 0.25`.
6. *Decision*: **auto_matched** when the best score is ≥ 90, at least 8 points ahead of the
   second candidate, with no review flag (a `review` characteristic in conflict such as the
   colour, or dimensions known on one side only / not comparable). A best score ≥ 99
   (`thresholds.certain`: title and dimensions identical) is automatic even when the second
   is closer than 8 points, with a `close_runner_up` reason (`warning: true`), unless the
   second also reaches 99 (nothing tells them apart). **needs_review** from 75 (or ≥ 90 but
   ambiguous/flagged); **no_match** below 75. Each decision stores its reasons.

Calibration note: on real products voyage-4 separates product *families* well but barely
separates two *sizes* of the same product (0.96 vs 0.93), and the lexical score ignores
numbers: the dimensions (an exact comparison) are what distinguish "equivalent" from
"similar". The score is a similarity score, **not a
probability**; all thresholds and weights are starting points in `config/matching.yaml`,
to tune with the validated/rejected decisions (query above).

**Changes and reruns**:

* `product_hash` (raw title, descriptions, category name, normalized attributes) is stored on
  each match for the competitor product and for its Antalis product. A match becomes
  **`deprecated`** when one of them changed, or its Antalis product went `missing` or was
  deleted, with `previous_status`, the former association and a `deprecated` history event
  kept, and a `matches_deprecated` **notification** (one per competitor and detection).
  Detection runs at the end of every catalog sync / price refresh job (for the synced
  competitor's matches, or for every competitor when Antalis was synced), at the start of
  every matching run, and after a product update/deletion through the API.
* Embeddings are cached **per text, globally** (`text_embeddings`): a text already embedded
  for any product of any competitor is never sent to Voyage again, whatever the run mode, so
  rerunning `mode=all` after a scoring change costs no embedding, and a title changed then
  changed back is not paid twice. Only `"reembed": true` on a run (`--reembed` in the CLI)
  embeds its texts again and replaces the cached vectors (normally never needed). Each batch
  is committed (an interrupted run keeps what it paid for).
* A change of `config/matching.yaml` (or of the competitor's `matching` options, or
  `ENGINE_VERSION`) changes `config_hash`: the next incremental run recomputes the engine
  results, without deprecating anything.
* `POST /api/v1/matching/products/{id}/reset` sends a match back to the engine.

**Voyage AI quota**: the account has a payment method (the 200M free tokens stay free):
Voyage allows **2000 requests / 8M tokens per minute** for `voyage-4`. The embedder paces its
requests to these limits (`embedding.requests_per_minute` / `tokens_per_minute` in the
config): ~100 tokens per product, so a full catalog is embedded in a few minutes at most;
reruns only embed changed texts. Without a payment method Voyage drops to 3 RPM / 10K TPM
(~1 h per 6000 new products): lower the config values accordingly.

---

## 6. Scrape.do integration

* Request format (from the official docs): `GET https://api.scrape.do/?token=…&url=<encoded>`
  plus `timeout` (ms), optional `render=true`, `super=true`, `geoCode=fr`.
* Antalis needs neither rendering nor residential proxies: plain requests cost 1 credit
  (`Scrape.do-Request-Cost`), the HTML contains everything (see observations doc).
  `geoCode=fr` is set because the site is French.
* Status handling: 2xx ok; 404/410 → `TargetGoneError`; 401 (no credits / bad token) →
  `FatalFetchError`, the run stops; 400 → error, not retried; 429/5xx/502/510 and network
  errors → retried with exponential backoff + jitter (honours `Retry-After`), up to
  `max_attempts` (4). Credits used are summed from `Scrape.do-Request-Cost` and stored per run.
* A 200 response that is not a recognisable page of the site (block page, error page,
  truncated HTML) raises `UnexpectedPageError` in the adapter and is recorded as a failure,
  never as an empty category.
* Secrets: the token is only passed as a query parameter by httpx; the client never logs
  request URLs, httpx's own request logging is silenced, a logging filter masks the token
  (and any `token=` parameter) in every log record and exception text, and error messages
  are redacted. Fixtures contain target HTML only.

### Rate limits

* `request_interval_seconds` is the minimum time between request starts (one request at a
  time, retries included): a request that took longer is followed at once, a faster one
  waits for the rest. Antalis: 4 s (its `robots.txt` asks for `Crawl-delay: 10`; requests
  come from rotating Scrape.do IPs). Packdiscount: 0.5 s (no crawl delay), so its pace is
  the Scrape.do round trip. `--request-interval` can override it for a run.
* Scrape.do account limits (from `/info`): 50 concurrent requests; the engine uses 1.
* Cost: ~1 credit per page. A full Antalis packaging crawl is ≈ (listing pages + product
  pages) credits; at 4 s/request, about 900 pages per hour.

### Server deployment (Coolify)

`Dockerfile` + `docker-compose.coolify.yml` (Coolify "Docker Compose" build pack), one
image, three services:

* `db` – PostgreSQL 16 with a persistent volume; password generated by Coolify
  (`SERVICE_PASSWORD_POSTGRES`). Not exposed publicly.
* `api` – `catalog-scraper api` on port 8000, published by Coolify's proxy (HTTPS) at
  <https://api-haoyfwryqzh3rskh66g9evxy.185.252.233.123.sslip.io/api/v1> (domain generated
  from `SERVICE_FQDN_API_8000`, switched to `https://` in the Coolify service settings).
  Requires `API_KEY`.
* `worker` – `catalog-scraper worker`. **Does nothing until a job is requested** through the
  API (unless `AUTO_SCHEDULE=true`, which enqueues a catalog sync every
  `CATALOG_SYNC_INTERVAL_DAYS` and a price refresh every `PRICE_REFRESH_INTERVAL_HOURS`).
  Both services apply migrations at start-up (serialized by a PostgreSQL advisory lock).

Coolify environment variables: `SCRAPEDO_API_KEY`, `API_KEY` and `VOYAGE_API_KEY` (secrets;
without the Voyage key matching is disabled), optional `CORS_ORIGINS`, `AUTO_SCHEDULE`,
`WORKER_AUTO_RESUME`, `WORKER_MAX_CONCURRENT_JOBS`, `LOG_LEVEL`.

**Redeploying / restarting** the worker: on SIGTERM, running (or paused) jobs stop after the
current page and end `interrupted` (90 s grace period); a job killed without notice is
closed the same way when the worker starts again. The new worker then **restarts the
interrupted jobs by itself** (§2, `WORKER_AUTO_RESUME`): a catalog sync replays the category
pages already fetched (no request) and fetches only the remaining pages and products. Data
already written is kept (one transaction per page/product).

**Database migrations at deploy**: both services run `catalog-scraper db-upgrade` (`alembic
upgrade head`) when they start, serialized by a PostgreSQL advisory lock (`db/migrate.py`):
the first applies the new migrations in one transaction (a failure rolls everything back and
the container does not start), the second finds the schema up to date.

During a deploy the **previous worker keeps running** for a moment (it finishes its current
page, 90 s grace) while the new release migrates. Hence the rule for every migration:
**stay compatible with the previous release** (expand/contract) — add tables/columns
first; stop using a column in one release and drop it in a later one (e.g.
`products.antalis_reference`, unused since 0005, still present until a later migration
drops it). In addition, a worker that sees a schema migrated by a newer release
(`alembic_version` unknown to its code) starts no new job (`schema_outdated` in its
heartbeat) and only finishes the running ones. Take a `pg_dump` before deploying a
migration that drops columns or tables.

**Access**: the API is the management surface (and the frontend's backend); everything but
`GET /api/v1/health` needs the API key. PostgreSQL stays on the compose network only; for
ad-hoc SQL use an SSH tunnel. CLI commands can be run from the worker container terminal in
Coolify (`catalog-scraper runs`, `... prices --competitor antalis_fr --product 503623`).

---

## 7. Tests

```bash
uv run pytest                    # unit + parser tests + DB tests (DB tests skip if no TEST_DATABASE_URL server)
uv run pytest --live -m live     # live Scrape.do smoke tests (3 credits)
uv run ruff check src tests
```

* `tests/test_antalis_parsers.py` – Antalis parsing on saved pages (`tests/fixtures/antalis/`):
  page types, root/sub/listing/family navigation, pagination, SKU 503623 full parse,
  degressive tiers, block pages, missing sections.
* `tests/test_engine_db.py` – generic engine on PostgreSQL with the toy competitor:
  hierarchy without duplicates, idempotent re-runs adding new products, partial runs never
  flagging missing products, mass-missing safety net, price refresh independence,
  retry of failed products and of failed catalog pages, resume replaying cached listing
  pages, identity mismatch, no-price vs failure, quantity queries,
  plus an Antalis end-to-end sync of family HQ02953 on fixtures.
* `tests/test_scrapedo_client.py` – request format, retries, fatal errors, token redaction.
* `tests/test_jobs_db.py` – worker jobs: one job per competitor (service and database),
  price refresh only after a full catalog, dispatcher end to end, progress, cancel, pause /
  resume, shutdown/restart interruption, automatic resume after a redeploy (work done
  skipped, crash-loop guard), restart, old worker idle once a newer release migrated,
  failure guard, notifications, opt-in schedule.
* `tests/test_api.py` – the HTTP API: auth, worker lifecycle and refusals, product search,
  facets, CRUD, notifications read/unread, error format, `openapi.json` up to date.
* `tests/test_matching_engine.py` – matching without database: config validation, text and
  hashes, dimension parsing/scoring, product types and characteristics, TF-IDF, decisions
  on a sample catalog (auto / review / no match, exclusions, conflicts), Voyage client
  (payload, retries, key redaction), rate limiter, batching.
* `tests/test_matching_db.py` – matching on PostgreSQL with a deterministic fake embedder:
  prerequisites, first run, reruns reusing embeddings and recomputing nothing, deprecation
  (product changed, Antalis product changed / missing / deleted) with notifications and
  history, human decisions kept across reruns, rejected candidates never proposed again,
  decision taken during a run not overwritten, estimates, detection after a sync job.
* `tests/test_matching_api.py` – matching through the API and the worker: run, review queue,
  score breakdown, compare, validate/reject/reset, list filters and pagination, deprecated
  list and rerun, refusals.
* `tests/test_schema_db.py` – the migrations build exactly the SQLAlchemy models.
* `tests/test_live_scrapedo.py` – opt-in live checks.

---

## 8. Limitations / open points

See `docs/antalis-observations.md` for details.

* Prices are the **public anonymous** prices (HT). Customer-specific prices after login are
  out of scope.
* Product listing pagination was not observed on Antalis (max 14 cards sampled); it is
  handled generically and verified against the announced result count.
* Some products (machines, some papers) show **no price** — only a "Demander à être rappelé"
  (callback) button, also after JavaScript rendering — while their schema.org JSON-LD offer
  carries a price. With `record_undisplayed_json_ld_price: true` (default) that value is
  stored with `price_basis='unknown'`, `tax_mode='unknown'`, `metadata.displayed=false`,
  `metadata.tier_source='json_ld_offer'` (and a run-item warning); set the option to `false`
  to record these products as `no_price` instead. Filter them out of comparisons with
  `metadata->>'tier_source' <> 'json_ld_offer'`.
* `is_promotional` is unknown (NULL) for tiers of a tiered product in promotion.
* The Antalis site has internal inconsistencies that are stored as-is (e.g. `Quantité sur
  palette` 1280 vs palette sales unit 540 for SKU 503623).
* The crawl is sequential by design (crawl delay). A full sync of the packaging sector
  takes hours (~3 h for Antalis): start it as a worker job.
* A paused job is held in memory: a worker restart ends it `interrupted` (and the automatic
  resume restarts it running, not paused). Pauses have no time limit.
* Resuming a catalog sync replays the category pages cached within `resume_within_hours`
  (48 h by default; automatic resumes cover the whole interrupted job): an older
  interruption crawls them again.
* The API has a single shared key (no users/roles).
* Product matching: thresholds and weights are initial values, calibrated on a small sample
  only; tune them with the human decisions. The keyword taxonomy (`product_types`,
  `features`) covers packaging; extend it when a competitor sells other families. Vector
  search is done in memory (numpy): fine for tens of thousands of reference products; for
  much bigger catalogs, move the vectors to pgvector. Products of a competitor whose
  details are not parsed (no full catalog sync yet) cannot be matched.
