# Cenpac France: observations (2026-10-22)

Source of truth: the Intershop REST API behind `www.cenpac.fr` for navigation (categories,
families, variations), and the public variant web page `https://www.cenpac.fr/-/-/p<sku>` for
the sellable product (attributes, every price tier, sales unit, promo price). Saved responses
are in `tests/fixtures/cenpac/` (see its README). The adapter is
`src/competitor_catalog/adapters/cenpac/` and the configuration is
`config/competitors/cenpac_fr.yaml`. Raw captures and the analysis are kept outside the repo.

Legend: **[confirmed]** seen in the responses; **[assumption]** inferred, not proven.

## 1. Why the API for navigation and the web page for products

* **[confirmed]** The REST API gives the category tree, the category product lists and the
  variations of a family in plain JSON, one request each. It has no labelled attributes, no
  sales unit and no per-tier price for a variant: the attributes are numeric codes without
  labels, and `productprices` returns tiers without units.
* **[confirmed]** The variant page `https://www.cenpac.fr/-/-/p<sku>` gives, in one plain
  request (1 Scrape.do credit), the name, every attribute with its label, every price tier, the
  sales unit ("la caisse", "le colis de 1000"...), the struck promo price, the descriptions,
  the images, the category ids, the master SKU and an explicit "Prix non disponible" marker.
  It was checked on 25 variant pages.
* **[confirmed]** The variant page's canonical link points to the **family** page. The stored
  product URL is therefore always built from the SKU (`https://www.cenpac.fr/-/-/p<sku>`), never
  from the canonical link.

## 2. Access

* **[confirmed]** Hosts: API `https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-`,
  web `https://www.cenpac.fr`. No separate API host, no authentication, prices are public.
* **[confirmed]** `robots.txt` has no `Crawl-delay` and disallows nothing under
  `/INTERSHOP/rest/` or `/-/-/p<sku>` (it disallows `/INTERSHOP/web/`, filter and sort
  parameters, among others).
* **[confirmed]** `amount` is capped at **200** per page (`Bad Request (amount must not exceed
  200)`). Paged lists carry `total`, `offset` and `amount`; a list that fits in one page carries
  none of them, and then it is complete.
* **[confirmed]** Errors are plain text, not JSON (`Not Found (Product with SKU '...' not found
  in channel 'Cenpac-Front')`). The adapter treats a non-JSON body on an API URL as an unexpected
  page, never as an empty listing.
* **[confirmed]** **WAF block page**: the site sometimes answers **HTTP 200** with a page titled
  `Request Rejected` ("Access denied", about 3 KB), on the API and on web URLs alike. It came on
  2 of 3 fetches of the same variations list, while the following fetches of the same resource
  succeeded. **Unknown SKUs on the web always get this page** (it is not a 404).
  The Scrape.do client retries a 2xx body containing one of `options.block_page_markers`
  (`<title>Request Rejected</title>`) like a 5xx (`http/block_pages.py`, wired in
  `app.build_fetcher`). After the last attempt the item is `failed`, never `not_found`.
  The adapter still raises `UnexpectedPageError` on that page if it reaches it.
* **[observed, environment]** The first connection of a new process is sometimes dropped
  (ConnectTimeout). The client retries it.

## 3. Model: tree, lists, masters, variations

* **[confirmed]** The "Les produits" root (`Cenpac`) has 22 children and 414 nodes in total,
  depth 3, 316 leaves. Node identity is the **id path** (`Cenpac/1000/1004/1004a`): a bare id
  such as `1004a` is refused by the API (404).
* **[confirmed]** `categories/Cenpac/<path>/products` lists the **masters** (`VARIATION_MASTER`)
  of the category **and its whole subtree** (`categories/Cenpac/1000/products`: `total` 67).
  No variant appears in any category list.
* **[confirmed]** `products/<sku>/variations` lists the sellable variants of a family. It is
  **paged** (default 50): `11226` has 189 variants, read as `amount=200` in one page, or in
  pages with `offset`. A family of exactly 50 returns 50 items without `total`.
* **[confirmed]** The flat list `/products` (`total` 4271) is **polluted**: legacy duplicates,
  items without name or category, a catalogue master with the default category
  `NegotiatedPrices`, and SKUs of several shapes. It is not used. The masters of the tree (1687
  in total) are the source.
* **[confirmed]** Catalogs other than `Cenpac` (`NegotiatedPrices`, `news`, `promos`...) are
  not crawled. `NegotiatedPrices` holds customer prices.

## 4. Scope: excluded branches and the home-category rule

* **[confirmed, user decision 2026-10-22]** Twelve top-level branches are out of scope and
  listed in `options.excluded_top_level_category_ids`: 12000, 8000, 21000, 14000, 16000, 10000,
  23000, 22000, 15000, 19000, 11000, 13000. Their categories are never fetched and never stored.
* **Categories**: the root page drops the excluded children. `is_in_scope` also refuses any
  category under an excluded branch, as a safety net.
* **Families (masters)**: a family's **home category** is the master's
  `defaultCategory.categoryPath`, read from the ids. A family is in scope only when that path
  starts with `Cenpac`, has at least two ids, and its top-level id is not excluded. A family
  without `defaultCategory`, or whose path starts with another catalog, is out of scope. The
  variant's own category is **not** used for scope: it can differ (605103 is in 13000/13010 on
  its page, while its family 11154 is in 1000).
* **[confirmed, cross-listing evidence]** Kept categories also list families whose home is
  excluded: 08065 (home 8000, listed by 17000 and 20000), 15014 (home 8000, listed by 17000),
  16419 (home 11000, listed by 17000 and 20000), 04001 (home 16000, listed by 17000), 20841
  (home 12000, listed by 20000 and 14000). Kept homes are confirmed for 13311 and 13947 (5000),
  12405 and 12205 (6000), 34309, 08050 and 08502 (7000), 11305 (2000), 17134 (4000) and 34357
  (3000).
* **Mechanism, no engine change**: a category product list emits every listed family URL. The
  engine fetches the family page (1 credit) and calls `adapter.is_in_scope`. An out-of-scope
  family is recorded as `skipped` ("outside the crawl scope"): nothing is stored and its
  variations are never queued. This is not a failure, so the listings stay complete. A listed
  URL that is not stored is ignored when the listings are saved, so there is no fake category
  and no dangling link.
* **[confirmed]** Each out-of-scope cross-listed family costs one request.

## 5. Main and extra categories

* **[confirmed]** There is no flag for main or extra categories. Merchandising lists
  (`Meilleures ventes`, `Emballez responsable !`, `Offre du moment`...) are top-level categories
  that list families whose home is elsewhere. Their families are handled by the scope rule
  (section 4), not by a category flag.
* **[confirmed]** Merchandising sub-categories also exist inside the main branches (for example
  `1000/1006/1006a`, 25 masters listed). Their families are in scope when their home is kept.
* **[confirmed]** The merchandising lists are the largest ones (`20000`: 640 masters, `15000`:
  618), so they are where most out-of-scope cross-listed families come from.

## 6. Identifiers

* **[confirmed]** Category `external_id` = the id path joined with `/`, root included
  (`Cenpac/1000/1004/1004a`). A path always contains `/` (except the root), so it cannot be
  confused with a master SKU. The category URL is built from the path.
* **[confirmed]** Family `external_id` = the master SKU as a string, leading zeros kept
  (`06703`, `06601`, `01902`).
* **[confirmed]** Variant `catalog_reference` = `external_id` = the SKU read from
  `span[itemprop=productID]` on the page (for example `361394`, `06612070`, `IMP001`).
  `parse_prices` returns the same URL and reference, so a price refresh can check the identity.
* **[confirmed]** The SKU `*****` is encoded in the URL (`/products/%2A%2A%2A%2A%2A`).
* **[confirmed]** Public product slugs are not in the API. Only the `/-/-/p<sku>` form is used.

## 7. Prices

* **[confirmed]** Source: `div[data-degressiveprices-config]` whose `data-degressiveprices`
  equals the SKU (one per page; the cross-sell cards use another attribute name). Its JSON
  holds the minimum order quantity (`qty`, in sales units) and `priceslist` (quantity, optional
  discount `remise`, optional struck price `oldprice`, price text such as `3,21 €`).
* **Sales unit** (**[confirmed]**): the `span.whitespace-no-wrap` of the offers block ("la caisse",
  "le colis", "la bobine", "le rouleau", "la boîte", "la palette", "le sac", "la grue", "l'unité").
  The pieces per unit come from the text after it ("le colis de 1000"). The order multiple is
  the span "vendu par colis de 20" (step 20) or "vendu à l'unité" (step 1).
* **Tiers counted in colis** (**[confirmed]** on the 25 pages: 8 use colis, the others sales
  units): the page's Handlebars template renders `{{ this.qty }} colis et +` when tiers are counted in
  colis, and `{{ this.qty }}  et +` when they count sales units. Adhesives are priced per
  rouleau, sold by colis of 36, with tiers "1 / 3 / 5 colis".
* **Stored unit** (decision): `price_unit` = the sales unit noun without its article. The price
  is for one sales unit: `price_unit_quantity = 1`. Colis-counted tiers are converted to sales
  units (`qty x colis size`) when the size is known, so `quantity_unit == price_unit`. Pieces per
  colis go to `metadata.units_per_price_unit`, not to `price_unit_quantity`: the tiers count
  colis and the price is for one colis.
* **Bands**: `min_quantity` = tier quantity (converted), `max_quantity` = the next tier's
  minimum, the last tier is open. When the minimum order quantity is above the first tier's
  minimum and below its maximum, the first tier starts at the minimum order quantity (a tier
  "1" with a minimum of 10 gives [10, 100)); the raw value is kept in `metadata.site_quantity`.
* **Tax** (**[confirmed]**): prices are **excl. VAT** ("HT" in the table header "Prix unitaire
  HT"). They equal the API `net` values. `tax_mode = excl_tax`; a page without "HT" would be
  `unknown` with a warning.
* **Promotion** (**[confirmed]**): a tier with a struck price (`oldprice`) is
  `is_promotional = True` (for example 24,94 becomes 7,80, -75 %). A volume discount alone
  (`remise`, -15 %...) is stored in `metadata.discount`.
* **No price** (**[confirmed]**): the offers block shows "Prix non disponible, contactez nous."
  (`[itemprop=price]` without a `content` attribute). The variant is stored as `no_price`
  with that text as the reason, even when a tier list is present. An empty tier list gives the
  same result. Tiers with a zero price are dropped with a warning.
* **[confirmed]** 40 % of the variants of a family can be unpriced (customer variants such as
  "... INTERMARCHE"). They are stored as `no_price` and are not treated as failures.

## 8. Attributes

* **[confirmed]** The labels come from the variant page (`Hauteur extérieure (mm)`,
  `Paquet de`, `Epaisseur (µ)`...). The REST API only gives 4-digit codes.
* **Mapping**: `options.attribute_mapping` maps the page labels to normalized keys
  (`length_mm`, `width_mm`, `height_mm`, `roll_length_m`, `thickness_um`, `bundle_quantity`,
  `pallet_quantity`, `material`, `color`, `quality_code`, `print`...). Matching ignores case
  and accents; the first match wins.
* **Zero placeholders** (**[confirmed]**): some pages show a dimension as `0` (516476 has
  "Longueur extérieure (mm) 0"). Zero values are kept as raw attributes but never feed the
  normalized values.
* **Dimensions** (**[confirmed]**): `Dimensions intérieures Lxlxh (mm)` ("545 x 345 x 325") is
  split into `inner_length_mm`, `inner_width_mm` and `inner_height_mm` (`options.dimension_attributes`).
* **Encoding**: the `µ` of `Epaisseur (µ)` is U+00B5 (the page's entity decoded by lxml).

## 9. Cost and duration (estimate, not measured on the full catalog)

* **[confirmed]** Kept branches: 256 category pages (255 nodes and the root) plus about 10 extra
  pages of product lists (`estimate_hints.listing_pages` = 2700 with the variation and master
  pages). About 1 000 masters in scope plus about 200 out-of-scope families (1 request each),
  about 1 000 variations pages.
* **[assumption]** About 12 500 variants (22 985 online products minus 1 687 masters, about 60 %
  in the kept branches). One web page each. `estimate_hints.products` = 12500.
* **Total**: about 15 000 requests and credits for a full sync, about 12.5 h at 3 s per request
  on average. A price refresh is one page per stored variant, about 12 500 requests (about 10 h).
* Settings: `request_interval_seconds: 3.0` with `request_jitter_seconds: 1.0` (2-4 s between
  request starts; a fixed 1 s got the WAF to answer with a block page in a live test), `max_attempts: 4`,
  Scrape.do plain request with `geo_code: fr`, 1 credit each.
* Not measured. The first full sync gives the real counts; update `estimate_hints` then.

## 10. Local validation (2026-10-22)

* **[confirmed]** 25 variant pages were read (099, 118-121, 126-137, 161-168) across the
  branches 1000, 2000, 3000, 4000, 5000, 6000, 9000, 11000 and 12000, priced and unpriced.
* Unit tests on the saved pages and responses: `tests/test_cenpac_parsers.py`, and
  `tests/test_block_pages.py` for the retry. The engine path on fixtures is in
  `tests/test_engine_db.py` (a family in scope, an out-of-scope family skipped, a price refresh).
* Not done: a full catalog run (deliberately not run in development). Planned for the review
  phase: a live smoke test of about 40 credits (one leaf category with 6 families, two family
  URLs of which one is out of scope, a price refresh of 2 variants).
* Adding `cenpac_fr` to `WORKER_COMPETITORS` in Coolify enables automatic scheduling. It is not
  in the default list, so nothing runs by itself.

## 11. Known gaps

* **[assumption]** Removed or unknown variants get the same WAF page as a real block (unknown
  SKUs always do). With the retry, a removed variant costs `max_attempts` requests and ends
  `failed`, never `not_found`, in a price refresh. A later full sync stops listing it and flags
  it as missing. Accepted for now.
* **[assumption]** Colis rules rest on the page template (`colis et +`), verified on 25 pages in
  9 branches. The odd table header "( colis de 3200 )" is ignored. If a full sync shows
  "colis size unknown" warnings, review them.
* **[confirmed]** The variant breadcrumb is the variant page's own category. It is dropped
  when that category is outside the kept branches (605103): the breadcrumb is then limited to
  the family.
* **[confirmed]** A non-master `Product` in a category list is ignored with a warning. None was
  seen in the category lists of the analysis.
* **[confirmed]** `variations` `total` is not checked by the engine (continuation pages), as on
  Packdiscount.
* **[assumption]** The sort order of category lists across days is not verified. Pagination
  happens within a run, so this only matters for resumes.
* **[confirmed]** Matching: a price per colis (`price_unit_quantity` 1) can be compared with a
  price per piece elsewhere. This is an existing limitation (Packdiscount has it too); the
  pieces per unit are kept in `metadata.units_per_price_unit` for a later fix.
* **[assumption]** A shared Intershop base class with the Packdiscount adapter is postponed.
  Only small JSON helpers are duplicated.
