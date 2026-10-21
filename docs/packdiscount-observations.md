# Packdiscount France: observations (2026-10-20)

Source of truth: the Intershop REST API behind `www.packdiscount.com`
(`https://api.packdiscount.com/INTERSHOP/rest/WFS/Packdiscount-FR-Site/rest`). Saved
responses are in `tests/fixtures/packdiscount/`. The adapter is
`src/competitor_catalog/adapters/packdiscount/adapter.py`.

Legend: **[confirmed]** seen in the responses; **[assumption]** inferred, not proven.

## Why the API and not the HTML

* **[confirmed]** The web shop is an Angular app with server-side rendering, but the price
  block is not in the HTML. Only "À partir de 0,60 €" and some promo badges are visible.
  Parsing it would mean either JavaScript rendering or guessing from partial markup.
* **[confirmed]** The JSON API answers every request in **1 Scrape.do credit** with plain
  datacenter requests (no render), returns structured fields (SKU, prices, minimum order
  quantity, packing unit, stock, dimensions) and needs no authentication.
* **[confirmed]** `api.packdiscount.com/robots.txt` is `Allow: /` with no `Crawl-delay`.
  `www.packdiscount.com/robots.txt` has no `Crawl-delay` either; it disallows `?filters=`,
  `?sorting=`, `/search/`, `/basket`, `/checkout/`, `/account`. The crawler never requests
  those paths; it reads the API only.

## Access

* **[confirmed]** `amount` is capped at **200** per page (`400 amount must not exceed 200`).
  Paged lists carry `total`, `offset`, `amount`.
* **[confirmed]** Product details accept `?expand=prices,images,attributes,availability,variations,variableVariationAttributes`
  (one call returns everything the adapter reads). The `variations` endpoint **ignores**
  `expand`: variant prices need one call per variant.
* **[confirmed]** Responses are UTF-8 but declared without a charset. httpx guessed another
  encoding, so the Scrape.do client now decodes bytes as UTF-8 when no charset is given
  (`http/scrapedo.py`, `_decode`). Without this fix, names such as "à" were stored as `�`.
* **[observed, environment]** The first connection of a new process is sometimes dropped
  (ConnectTimeout). The client retries it.

## Model: categories, families, products

| Intershop resource | `type` / `productTypes` | what it is for the scraper |
|---|---|---|
| `categories` | `Categories` | root: its children are the top-level categories |
| `categories/<id path>` | `Category` | category node; `subCategories` gives the children |
| `products` (flat list) | `ResourceCollection`, 1352 items | every master and simple product; **variants are not in this list** |
| `products/<sku>` | `Product`, `VARIATION_MASTER` | **family**: not sold. Kept as a category (`product_family`) |
| `products/<sku>/variations` | `Variations` | the sellable variants of a family (`VariationLink`) |
| `products/<sku>` (variant) | `Product`, `VARIATION_PRODUCT` | **the product**: SKU, price, minimum quantity, dimensions |
| `products/<sku>` (simple) | `Product`, `PRODUCT` | a product sold as it is |

* **[confirmed]** In a sample of 15 flat-list products, 14 were `VARIATION_MASTER` (families)
  and 1 was a simple product. The first 12 items of the list gave 11 masters and 1 simple
  product. Families had 5 to 7 variants in the sampled cases.
* **[confirmed]** A family's own `salePrice` / `minSalePrice` are the lowest variant price,
  not a price a customer can pay for the family itself. Its description says
  "Voir toutes les références". Only variants are stored as products.
* **[confirmed]** A category's identity is its **id path** (`2/26`), not its id. The id `26`
  exists twice: `categories/26` ("Emballages Couleurs") and `categories/2/26` ("Carton simple
  cannelure"). The adapter builds URLs from the id path.
* **[confirmed]** The `uri` in `defaultCategory.categoryPath` is wrong for nested nodes
  (`categories/26` for the node `2/26`). The adapter ignores these URIs and rebuilds them from
  the ids.
* **[confirmed]** `categories/2` has 23 `subCategories` and `subCategoriesCount` = 23: the
  children list is complete.
* **[confirmed]** All 16 products of category `2/26` are in the flat list, which suggests the
  flat list is the complete product set (not verified for every category).
* **[confirmed, 2026-10-21]** `categories/<id path>/products` lists the masters and simple
  products of a category, sub-categories included (`categories/2/products`: `total` 64, pages
  of `amount` 50 by default; `amount=200` accepted). A small list (`2/26`: 16 items) comes
  without `total`/`offset`/`amount`. Every crawl reads it (category pages list no product):
  it is how the extra categories below are known.

## Main and extra categories

* **[confirmed, 2026-10-21]** A product's own category is its `defaultCategory`; other
  categories also list it. Example: family `P00175` (variants MCFA35P, MCFA31P, MCFA30P) has
  `defaultCategory` `1/19` (Emballages E-commerce › Vente par palette, the web URL
  `…-prdP00175-ctg1.19`) and is also listed by `19/196` (Vente à la palette › Palette caisse
  carton fond automatique): `categories/19/196/products` returns it.
* **[confirmed]** Measured on all 234 categories and the 593 products they list: 105 categories
  are the `defaultCategory` (or an ancestor of it) of at least one product; the 129 others
  only list products filed elsewhere. They include the whole top-level categories 14 to 29
  (Cadeau, Export, Vente à la palette, Promotions, Écologique, Couleurs, Blancs…) and 40
  sub-categories of the main ones (`1/10`-`1/18`, `2/311`-`2/314`, `5/58`…).
* **[confirmed]** The category attribute `ExtraCategory` is not reliable: `true` on 75
  categories (never a `defaultCategory`, correct), but `false` on 40 categories that are never
  one either, and absent on the top-level categories 14-29. The scraper ignores it: a category
  is `extra` when its subtree owns no product but lists some (`categories.role`), and its
  listings are stored in `category_listings`.
* **[confirmed]** Live check (2026-10-21, direct API, test database): a crawl of `1` and `19`
  (518 requests, 350 products) gave `1` and `1/19` main, `1/12` main (owns one product),
  `1/10`-`1/18` and the whole `19` subtree extra; `19/196` shows the 3 MCFA…P variants and
  `P00175` stays under `1/19`.

## Identifiers

* **[confirmed]** SKU = `sku` (e.g. `MCFA23`, `P00041`, `263481`). Stored as
  `catalog_reference` and `external_id`. `productMasterSKU` links a variant to its family.
* **[confirmed]** Product URL used by the engine: `.../products/<sku>?expand=...` (the expand
  is part of the canonical URL, so the same string is fetched and stored).
* **[confirmed]** Public web URLs exist (`/caisses-cartons-et-boites/.../prd<SKU>-ctg2.26`) but
  are not stored: their slug is not in the API.

## Prices

* **[confirmed]** `salePrice` (price the customer pays) and `listPrice` (reference price),
  both `{"value", "currency": "EUR"}`. `is_promotional` is set when sale < list.
* **[confirmed]** `minOrderQuantity` is the lower bound of the price band (e.g. 20 for MCFA23);
  `stepOrderQuantity` equals it on the samples; `packingUnit` is `UN` on variants and empty on
  families. Stored as one band `[min, ∞)` per unit (`price_unit = UN`).
* **[confirmed]** No product-level degressive tiers in the API (`prices` expand returns only
  `listPrice` and `salePrice`). The web shows **basket-level** volume discounts
  ("-10% dès 300€HT", "-15% dès 450€HT", "-20% dès 800€HT"), not per-product ones.
* **[assumption]** Prices are **excl. VAT**: the site writes "HT" next to basket thresholds and
  "Livraison gratuite Dès 250€ HT". The API has no tax field, so `tax_mode` is stored as
  `unknown` until verified (for example against the basket total).
* **[confirmed]** Products without a `salePrice` value are stored with `price_found = false`
  (not a scrape failure).

## Dimensions and attributes

* **[confirmed]** Variants carry their dimensions as `variableVariationAttributes`
  (`caisse_longueur_interieur` = `30.0` in cm, and so on). The config maps the internal ids
  to labels in cm; the normalization converts them to mm (`inner_length_mm` = 300).
* **[confirmed]** Mapped: inner length/width/height, maximum height, pallet quantity, quality
  code (`PC20`), bundle quantity (`Colis de`). Other attributes are stored raw.
* **[confirmed]** Non-box families (e.g. "Accessoires vestiaire industrie") have no
  dimension attributes: their normalized attributes are empty, and their raw attributes are
  kept.
* **[confirmed]** `shortDescription` of a variant is only the family link text; it is stored
  as an empty description. `longDescription` holds the real text (HTML converted to text).
* **[confirmed]** Images come in 4 renditions (`S`, `M`, `L`, `ZOOM`); all are stored with
  their URL (Cloudinary).

## Cost and duration (estimate, not measured on the full catalog)

* Listing calls: 1 root + ~234 category nodes + ~240 category product lists + 7 flat-list
  pages + 1352 product checks (every flat-list item is classified) + ~1200 variation pages.
* Variant calls: ~5 per family (one per variant, for the price): ~6000 in total.
* Total ≈ **9 000 credits** and ≈ **9 000 requests** (the category product lists add ~250). The first full run on the server made
  7 501 requests in 4 h 22 at `request_interval_seconds: 2` (2.1 s/request). The interval is
  now 0.5 s, so the pace is bounded by the Scrape.do round trip instead.

## Local validation (2026-10-20)

* Unit tests on saved responses: `tests/test_packdiscount_parsers.py` (30 tests).
* Live: 15 products through the real Scrape.do client (sample, no database).
* Database: `catalog-sync --root-url` on 3 products (family P00052, family P00001, simple
  263481) plus `--max-products 3`; `price-refresh --product BCL5 --update-details`.
  Stored: 2 families, 13 products, 13 observations, 0 failures.
* Not done: the full catalog run (deliberately left to the server).

## Known gaps

* **[assumption]** VAT basis unknown (see Prices).
* **[confirmed]** A scoped run (`--root-url` on a product) creates the family categories but
  not the category of a simple product (its category is only known from the product page).
  A full run creates the whole tree from the category root, so it is not affected. Likewise a
  crawl of an extra category alone stores the families it lists, but a family whose own
  category was never crawled has no parent until a full run.
* **[assumption]** Families with more than 200 variants would be truncated; the variations
  page warns when the page size is reached. None seen so far.
