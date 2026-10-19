# Antalis fixtures

Raw HTML returned by Scrape.do (plain request, no rendering) for www.antalis.fr on
2026-10-19. They are the target pages only: no Scrape.do token, headers or cookies.

| file | page type (`contextDataLayer`) | URL |
|---|---|---|
| sector_cat-3.html | SECTOR | /eshop/packaging-machines-emballage (301 -> /cat-3) |
| sub_hq02946.html | SUB_CATEGORY | .../caisses-carton-palettes/sub-hq02946 |
| plp_hq02947.html | PRODUCT_LISTING (14 families) | .../caisse-carton-ondule/plp-hq02947 |
| plp_hq03009.html | PRODUCT_LISTING (10 families) | .../ruban-adhesif-emballage/plp-hq03009 |
| family_hq02953.html | PRODUCT_DETAILS (4 SKUs) | .../caisse-carton-a-hauteur-variable-a-fond-automatique/pdp-hq02953 |
| family_hq02957_p1.html | PRODUCT_DETAILS page 1/7 (132 SKUs) | .../caisse-carton-simple-cannelure/pdp-hq02957 |
| family_hq02957_p2.html | PRODUCT_DETAILS page 2/7 | ...pdp-hq02957?no=20 |
| sku_503623.html | SKU_DETAILS, single promotional price | .../sku-503623 |
| sku_506212.html, sku_348426.html, sku_506211.html | SKU_DETAILS (rest of family HQ02953) | |
| sku_586283.html | SKU_DETAILS, 3 degressive price tiers | .../sku-586283 |

Refresh a fixture with:
`uv run catalog-scraper fetch --competitor antalis_fr <url> --out tests/fixtures/antalis/<file>`
