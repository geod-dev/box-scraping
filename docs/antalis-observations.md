# Antalis France e-shop: observations (2026-10-19)

Pages inspected through Scrape.do (plain requests, `render=false`): the sector root, a
sub-category, two product listings, two product families (one paginated, pages 1 and 2)
and six SKU pages. Saved copies are in `tests/fixtures/antalis/`.

Legend: **[confirmed]** seen in the responses; **[assumption]** inferred, not proven.

## Access

* **[confirmed]** Plain Scrape.do requests (datacenter proxy, 1 credit) return the full
  server-side rendered HTML with status 200. Scrape.do reports `Detected-Waf: AKAMAI`.
  JavaScript rendering is **not** needed: navigation, characteristics, prices and price
  tiers are all in the initial HTML.
* **[confirmed]** `https://www.antalis.fr/eshop/packaging-machines-emballage` answers
  `301` to `/eshop/packaging-machines-emballage/cat-3` (Scrape.do follows it and returns
  `Scrape.do-Resolved-Url`).
* **[confirmed]** `robots.txt` asks every user agent for `Crawl-delay: 10` and disallows
  `?ns=` (sorting), `resultPage`, `.pdf`, `/ws/html/secure/`... The crawler only requests
  category/listing/family/SKU pages and `?no=` pagination, and waits 10 s between requests.
* **[confirmed]** Prices are visible to anonymous visitors, labelled "Prix H.T." (excluding
  VAT, `data-vat-rate="20.00"`), currency `EUR` (`data-currency`).
* **[observed, environment]** From this workstation, the first TCP connection a new Python
  process opens to `api.scrape.do` is sometimes dropped (curl is unaffected); the client's
  10 s connect timeout + retry absorbs it.

## Page types and identifiers

Every e-shop page contains
`<script id="dataLayerScript" contextDataLayer="TYPE" pageDataModel="{json}">`.

| `contextDataLayer` | URL shape (informational only) | content | `pageDataModel.category_type_id` |
|---|---|---|---|
| `SECTOR` | `.../packaging-machines-emballage/cat-3` | cards of sub-categories | `HQ00000` **(placeholder, not an id)** |
| `SUB_CATEGORY` | `.../caisses-carton-palettes/sub-hq02946` | cards of listings / families | `HQ02946` |
| `PRODUCT_LISTING` | `.../caisse-carton-ondule/plp-hq02947` | cards of product families | `HQ02947` |
| `PRODUCT_DETAILS` | `.../caisse-carton-a-hauteur-variable-a-fond-automatique/pdp-hq02953` | **product family**: list of SKUs with prices | `HQ02953` |
| `SKU_DETAILS` | `...-pdp-hq02953/sku-503623` | the product (one SKU) | `HQ02953` = id of its family |

* **[confirmed]** `category_type_id` is a real hierarchy node id exposed by the page data
  layer; it matches the `hq…` token of the URL but is read from page data, not from the URL.
  It is stored as `categories.external_id`; the root (placeholder `HQ00000`) has none and is
  identified by its canonical URL.
* **[confirmed]** The same node can be linked with different slug paths
  (`/ruban-adhesif/ruban-adhesif-emballage/plp-hq03009` in SEO text vs
  `/ruban-adhesif-colles/ruban-adhesif-emballage/plp-hq03009` in navigation): slugs are not
  identifiers. Every page declares a `<link rel="canonical">`, used as the stored URL.
* **[confirmed]** `pdp-hq02953` is the **family** page; it is distinct from the SKU page
  `.../sku-503623`. Products are SKUs. Families are stored as categories of kind
  `product_family` and are the most specific category of their SKUs.
* **[confirmed]** The SKU reference comes from page data: JSON-LD `Product.sku`, the last
  breadcrumb item and the add-to-cart `productId` (all three agree on fixtures).

## Hierarchy (navigation)

* **[confirmed]** Each page has a schema.org `BreadcrumbList` (`nav.eshop-breadcrumb`):
  `Accueil > Packaging et Machines d'emballage > Caisses Carton & Palettes > Caisse Carton Ondulé > Caisse Carton à hauteur variable et à fond automatique > 503623`.
  The real hierarchy therefore has a *listing* level (`plp-hq02947`) between the
  sub-category and the family. Parents are taken from each page's own breadcrumb.
* **[confirmed]** Sector and sub-category pages show children as
  `div.eshop-card.eshop-card-sector` cards (title link + preview links). The preview on the
  root page is **not exhaustive** (e.g. `caisse-bois/plp-hq02988` only appears on the
  sub-category page), so every node page is visited.
* **[confirmed]** Some families hang directly under a sub-category (e.g. "Emballage
  Alimentaire" → `papier-cuisson-mc212/pdp-hq03304`) or are linked from the root card
  (`papier-etirable-master-in-performance/pdp-hq17360`).
* **[confirmed]** Editorial/SEO text, "Nos produits phares", promotions and cross-selling
  carousels also contain links (sometimes to other sectors or legacy slugs); they are not
  followed.

## Listings and pagination

* **[confirmed]** Product listings render family cards `div.card-group a.card`; the total is
  in `pageDataModel.results_number` (14 for `plp-hq02947`, all on one page).
* **[confirmed]** Family pages list SKU cards `div#skuLineForm div.card#skuline_<sku>`,
  20 per page, `Afficher 1 - 20 résultat(s) sur 132`, pagination links `?no=20`, `?no=40`...
  with a `li.eshop-pagination__page--next` link. Page 2 declares `?no=20` as canonical.
* **[confirmed]** No lazy loading / infinite scroll was found: all items of a page are in
  the HTML.
* **[assumption]** Product listings may paginate the same way when they have more cards
  than one page holds (not observed: largest sampled listing had 14 cards); the engine
  follows the same "next" link generically and checks `results_number` against the number
  of collected items, so a missed page makes the run `partial`.

## SKU page content

* **[confirmed]** JSON-LD `Product`: name, description, sku, images, brand
  (`Sans marque` on 503623), `offers.price` (the displayed price, or the *first* tier
  price on tiered products) and `additionalProperty` (dimensions with `unitCode: MMT`).
* **[confirmed]** Characteristics table `div.sku-attributes > div.sku-attributes-item`
  grouped (`Général`, `Dimensions`, `Composition`, `Performance`) with label/value spans.
  Labels carry units in brackets (`Largeur (mm)`, `Epaisseur (µ)`, `Longueur des rouleaux (m)`).
  The site spells some labels inconsistently (`Largeur interieure` vs `Longueur intérieure`).
* **[confirmed]** Extra characteristics: tab "Informations techniques" (e.g. `Stockage`),
  pictograms (`Certificat environnemental: Recyclé`, `FSC`), technical sheet PDF links.
* **[confirmed]** Images: gallery `div.eshop-vertical-gallery img` in two renditions per
  picture (`LOSQS_` small, `LOSQL_` large). Family pages expose more renditions in
  `data-images` JSON (`LOSQM_`, `LOSQXL_`, `LOTN_`, original `.eps`); they are not stored
  for SKUs because the SKU page does not show them (no URL invented).
* **[confirmed]** Sales units JSON (`data-salesUnits`): e.g. `paquet(s)` = 10 units,
  `palette(s)` = 540 units. Note: the characteristic `Quantité sur palette` says `1280.0`
  for the same SKU; both raw values are stored, none is "corrected".

## Prices

* **[confirmed]** Price block `div[data-price-bloc]`: `Prix H.T.`, price in `data-price`
  (dot decimal), per `/ 1 unité`. Promotions show a strikethrough list price
  (`€ 1,35`, `40,00%`) and the label `Prix Promo` (SKU 503623: 0.81 € HT/unit).
* **[confirmed]** Degressive prices: `div.scale-block div.item[data-variant="[36,108]"]`,
  e.g. SKU 586283: `1 boite` 3.07, `3 boites` 2.76, `5 boites` 2.29 €/unit. Bounds are in
  the price unit (unité); a box = 36 units. Bands are contiguous: `[36,108]`, `[108,180]`,
  `[180]`. Since the second band's label "3 boites" = 108 units, the upper bound is
  **exclusive** ([36,108)), the last band is open-ended. The headline price is then
  "à partir de" = lowest tier.
* **[confirmed]** Without scale block the page shows a single price; its minimum order
  quantity (`data-priceUnit.minOrderQty`, 10 units for 503623) is used as the band's lower
  bound (matches the first band minimum on tiered products).
* **[assumption]** On tiered products in promotion, it is unknown whether every tier is
  promotional: `is_promotional` is then stored as NULL (unknown) with the page flag in
  metadata.
* **[confirmed]** Price unit per tier: `/ 1 unité`, `/ 1 bobine(s)`, but also
  `/ 1 000 feuille(s)` (SKU 385709, baking paper: 101.47 € HT per 1000 sheets, bands
  `2 500`, `20 000`... sheets). Stored as `price_unit='feuille(s)'`,
  `price_unit_quantity=1000`.
* **[confirmed]** "Price on request" products (e.g. case sealers SIAT SK20 534997, stretch
  paper 628031): the price box only contains a "Demander à être rappelé." button
  (`GlobalHelper.openContactForm()`); no price is displayed, **also with JavaScript
  rendering** (tested with `render=true`, 5 credits). The page still contains an empty
  `data-price-bloc` template inside the add-to-cart modal (must not be mistaken for the
  price block). The JSON-LD offer nevertheless exposes a price (3999.0 / 49.5).
  **[assumption]** this is the same excl.-tax price per price unit as on products that
  display it; it is stored flagged `displayed=false`, basis/tax `unknown`.
* **[confirmed]** Some products have no `data-priceUnit` (no minimum order quantity): their
  single-price band then has no lower bound (`min_quantity` NULL).
