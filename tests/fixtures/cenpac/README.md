# Cenpac fixtures

| file | raw sample | URL | what it shows |
|---|---|---|---|
| `category_root.json` | `001` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac | root "Les produits", 22 children (280 KB) |
| `category_1000.json` | `111` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac/1000 | non-leaf node, 7 children |
| `category_1000_1004_1004a.json` | `021` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac/1000/1004/1004a | leaf |
| `products_1000_1004_1004a.json` | `005` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac/1000/1004/1004a/products | list without `total` (6 items) |
| `products_1000_offset_0.json` | `006` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac/1000/products | `total` 67, offset 0, amount 50 |
| `products_1000_offset_50.json` | `038` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac/1000/products?offset=50 | last page (17 items) |
| `products_9000_amount_200.json` | `046` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac/9000/products?amount=200 | `total` 413, amount 200 |
| `products_1000_1006_1006a.json` | `042` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/categories/Cenpac/1000/1006/1006a/products | merchandising sub-category list (25) |
| `product_master_11154.json` | `009` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/11154 | family in scope (1000/1004/1004a) |
| `product_master_06703.json` | `052` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/06703 | single-variant family, leading zero, in scope (3000) |
| `product_master_06601.json` | `060` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/06601 | leading-zero family, 3000 |
| `product_master_20841.json` | `058` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/20841 | home 12000: out of scope |
| `product_master_08065.json` | `139` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/08065 | home 8000, listed by 17000/20000: out of scope |
| `product_master_MA001.json` | `057` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/MA001 | home 13000/13010: out of scope |
| `product_master_stars.json` | `032` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/%2A%2A%2A%2A%2A | SKU `*****` (URL encoding), 13000 |
| `product_catalogue_cenpac.json` | `034` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/catalogue_cenpac | `PRODUCT` (not a master), `NegotiatedPrices` |
| `variations_11154.json` | `011` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/11154/variations | 4 variants, no `total` |
| `variations_06703.json` | `070` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/06703/variations | 1 variant (639405) |
| `variations_11226_offset_0.json` | `069` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/11226/variations | `total` 189, offset 0, amount 50 |
| `variations_11226_offset_50.json` | `080` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/11226/variations?offset=50 | offset 50 |
| `variations_11226_amount_200.json` | `079` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/11226/variations?amount=200 | 189 items, no `total` |
| `variations_12605_offset_50.json` | `081` | https://www.cenpac.fr/INTERSHOP/rest/WFS/Cenpac-Front-Site/-/products/12605/variations?offset=50 | empty page |
| `variant_361394.html` | `118` | https://www.cenpac.fr/-/-/p361394 | box: units, MOQ 20 = tier 1, 14 attributes |
| `variant_642271_colis_de_1000.html` | `099` | https://www.cenpac.fr/-/gobelet/p642271 | "le colis de 1000", colis tiers |
| `variant_643415_promo.html` | `119` | https://www.cenpac.fr/-/-/p643415 | Déstockage -75 %, one tier |
| `variant_605103_no_price.html` | `120` | https://www.cenpac.fr/-/-/p605103 | unpriced, page category 13000/13010 |
| `variant_11226001_moq.html` | `121` | https://www.cenpac.fr/-/-/p11226001 | MOQ 10 above tier 1 (1) |
| `variant_636554_tiers_by_colis.html` | `128` | https://www.cenpac.fr/-/-/p636554 | rouleau price, tiers in colis of 36 |
| `variant_06612070.html` | `127` | https://www.cenpac.fr/-/-/p06612070 | leading-zero film roll (µ, m) |
| `variant_639405.html` | `131` | https://www.cenpac.fr/-/-/p639405 | variant of 06703, promo -27 % |
| `variant_IMP001_no_price.html` | `135` | https://www.cenpac.fr/-/-/pIMP001 | "Prix non disponible" with tiers, no page category, no description section |
| `access_denied.html` | `174` | https://www.cenpac.fr/-/-/pZZNOTEXIST98 | WAF / unknown-SKU page |

Capture date: 2026-10-22.

JSON re-serialized on one line, HTML as returned by Scrape.do (plain request).
