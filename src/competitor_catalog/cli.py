"""Command line entry point: ``catalog-scraper <command>`` (or ``python -m competitor_catalog``)."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from competitor_catalog.app import build_context, build_fetcher, build_matching_runtime, session_factory
from competitor_catalog.core.config import load_competitor_config, load_competitor_configs
from competitor_catalog.db import repositories as repo
from competitor_catalog.db.models import Category, Product, ScrapeRun, ScrapeRunItem
from competitor_catalog.db.session import transaction
from competitor_catalog.logging_setup import configure_logging, register_secret
from competitor_catalog.services.catalog_sync import CatalogSync, CatalogSyncOptions
from competitor_catalog.services.price_refresh import PriceRefresh, PriceRefreshOptions
from competitor_catalog.settings import PROJECT_ROOT, load_settings


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = _parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    register_secret(settings.scrapedo_api_key)
    register_secret(settings.voyage_api_key)
    configure_logging(args.log_level or settings.log_level)
    return int(args.func(args, settings) or 0)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="catalog-scraper", description=__doc__)
    p.add_argument("--log-level", help="DEBUG, INFO, WARNING (default: LOG_LEVEL or INFO)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("db-upgrade", help="apply database migrations (alembic upgrade head)")
    s.set_defaults(func=cmd_db_upgrade)

    s = sub.add_parser("competitors", help="list configured competitors and sync them into the database")
    s.set_defaults(func=cmd_competitors)

    s = sub.add_parser("scrapedo-check", help="validate the Scrape.do integration (account info + 1 request)")
    s.add_argument("--competitor", default="antalis_fr")
    s.add_argument("--url", help="URL to fetch (default: first root URL of the competitor)")
    s.set_defaults(func=cmd_scrapedo_check)

    s = sub.add_parser("catalog-sync", help="OPERATION A: discover categories/products and sync details + prices")
    s.add_argument("--competitor", required=True)
    s.add_argument("--root-url", action="append", help="crawl only from this URL (repeatable; partial scope)")
    s.add_argument("--max-listing-pages", type=int, help="stop the listing crawl after N pages (partial scope)")
    s.add_argument("--max-products", type=int, help="only sync the first N discovered products (partial scope)")
    s.add_argument("--skip-products", action="store_true", help="only crawl categories and product URLs")
    s.add_argument("--allow-mass-missing", action="store_true", help="bypass the missing-products safety threshold")
    s.add_argument(
        "--resume-hours", type=float, help="do not refetch products whose details were synced less than N hours ago"
    )
    s.add_argument("--request-interval", type=float, help="override the configured delay between requests (s)")
    s.set_defaults(func=cmd_catalog_sync)

    s = sub.add_parser("price-refresh", help="OPERATION B: refresh prices of already known products")
    s.add_argument("--competitor", required=True)
    s.add_argument("--category", help="category id, external id or URL (whole subtree)")
    s.add_argument("--product", action="append", default=[], help="catalog reference (repeatable)")
    s.add_argument("--retry-failed", type=int, metavar="RUN_ID", help="only products that failed in that run")
    s.add_argument("--skip-fresh-hours", type=float, help="skip products priced less than N hours ago")
    s.add_argument("--include-missing", action="store_true", help="also refresh products flagged missing")
    s.add_argument("--update-details", action="store_true", help="also update description/characteristics/images")
    s.add_argument("--limit", type=int)
    s.add_argument("--request-interval", type=float, help="override the configured delay between requests (s)")
    s.set_defaults(func=cmd_price_refresh)

    s = sub.add_parser("prices", help="show latest prices / history of a product")
    s.add_argument("--competitor", required=True)
    s.add_argument("--product", required=True, help="catalog reference")
    s.add_argument("--quantity", type=Decimal, help="quantity (in the tier quantity unit)")
    s.add_argument("--history", action="store_true", help="all observations instead of the latest capture")
    s.set_defaults(func=cmd_prices)

    s = sub.add_parser("product", help="show a stored product")
    s.add_argument("--competitor", required=True)
    s.add_argument("--product", required=True, help="catalog reference")
    s.set_defaults(func=cmd_product)

    s = sub.add_parser("categories", help="print the stored category tree")
    s.add_argument("--competitor", required=True)
    s.set_defaults(func=cmd_categories)

    s = sub.add_parser("runs", help="list recent runs, or show one run with --run")
    s.add_argument("--competitor")
    s.add_argument("--run", type=int)
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser("match", help="match a competitor's products with the reference catalog (Antalis)")
    s.add_argument("--competitor", required=True)
    s.add_argument("--mode", choices=["incremental", "deprecated", "all"], default="incremental")
    s.set_defaults(func=cmd_match)

    s = sub.add_parser("worker", help="run migrations, then execute the jobs requested through the API")
    s.add_argument("--once", action="store_true", help="one scheduling pass, wait for the started jobs, exit")
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser("api", help="run migrations, then serve the HTTP API (uvicorn)")
    s.add_argument("--host", default=None, help="default: API_HOST or 0.0.0.0")
    s.add_argument("--port", type=int, default=None, help="default: API_PORT or 8000")
    s.set_defaults(func=cmd_api)

    s = sub.add_parser("export-openapi", help="write the OpenAPI schema (docs/api/openapi.json)")
    s.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "api" / "openapi.json")
    s.set_defaults(func=cmd_export_openapi)

    s = sub.add_parser("fetch", help="fetch one URL through Scrape.do and save the HTML (debug/fixtures)")
    s.add_argument("--competitor", required=True)
    s.add_argument("url")
    s.add_argument("--out", type=Path, required=True)
    s.set_defaults(func=cmd_fetch)
    return p


# --------------------------------------------------------------------------- commands


def cmd_db_upgrade(args: argparse.Namespace, settings: Any) -> int:
    from competitor_catalog.db.migrate import upgrade_database

    upgrade_database(settings.require_database_url())
    print("database schema up to date")
    return 0


def cmd_competitors(args: argparse.Namespace, settings: Any) -> int:
    configs = load_competitor_configs(settings.competitor_config_dir)
    factory = session_factory(settings)
    with transaction(factory) as session:
        for config in configs.values():
            competitor = repo.upsert_competitor(session, config)
            print(f"{competitor.id:>3}  {config.key:<20} {config.name:<25} enabled={config.enabled} {config.adapter}")
    return 0


def cmd_scrapedo_check(args: argparse.Namespace, settings: Any) -> int:
    config = load_competitor_config(settings.competitor_config_dir, args.competitor)
    with build_fetcher(settings, config, interval=0) as client:
        info = client.account_info()
        print(
            "account:",
            json.dumps(
                {
                    k: info.get(k)
                    for k in ("IsActive", "ConcurrentRequest", "RemainingMonthlyRequest", "MaxMonthlyRequest")
                }
            ),
        )
        url = args.url or config.root_urls[0]
        page = client.fetch(url)
        print(
            json.dumps(
                {
                    "url": url,
                    "status": page.status_code,
                    "final_url": page.final_url,
                    "bytes": len(page.text),
                    **{k: v for k, v in page.meta.items() if v is not None},
                },
                indent=2,
            )
        )
    return 0


def cmd_catalog_sync(args: argparse.Namespace, settings: Any) -> int:
    ctx = build_context(settings, args.competitor)
    options = CatalogSyncOptions(
        root_urls=args.root_url,
        max_listing_pages=args.max_listing_pages,
        max_products=args.max_products,
        skip_products=args.skip_products,
        allow_mass_missing=args.allow_mass_missing,
        resume_within_hours=args.resume_hours,
    )
    with build_fetcher(settings, ctx.config, interval=args.request_interval) as fetcher:
        result = CatalogSync(ctx.adapter, fetcher, ctx.factory, ctx.competitor_id).run(options)
    print(json.dumps({"run_id": result.run_id, "status": result.status, **result.counters}, indent=2))
    if result.incomplete_reasons:
        print(f"incomplete ({len(result.incomplete_reasons)} reasons), first ones:")
        for reason in result.incomplete_reasons[:10]:
            print("  -", reason)
    return 0 if result.status == "succeeded" else 2


def cmd_price_refresh(args: argparse.Namespace, settings: Any) -> int:
    ctx = build_context(settings, args.competitor)
    options = PriceRefreshOptions(
        category=args.category,
        references=args.product,
        retry_failed_run_id=args.retry_failed,
        skip_fresh_hours=args.skip_fresh_hours,
        include_missing=args.include_missing,
        update_details=args.update_details,
        limit=args.limit,
    )
    with build_fetcher(settings, ctx.config, interval=args.request_interval) as fetcher:
        result = PriceRefresh(ctx.adapter, fetcher, ctx.factory, ctx.competitor_id).run(options)
    print(
        json.dumps(
            {"run_id": result.run_id, "status": result.status, "selected": len(result.product_ids), **result.counters},
            indent=2,
        )
    )
    return 0 if result.status == "succeeded" else 2


def cmd_match(args: argparse.Namespace, settings: Any) -> int:
    from competitor_catalog.matching.service import MatchingError, MatchingOptions, MatchingRun

    ctx = build_context(settings, args.competitor)
    try:
        result = MatchingRun(
            ctx.factory,
            ctx.competitor_id,
            build_matching_runtime(settings),
            MatchingOptions(args.mode),
            competitor_options=ctx.config.matching,
        ).run()
    except MatchingError as exc:
        print(f"refused: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    print(json.dumps({"status": result.status, **result.counters}, indent=2))
    return 0


def _product(session: Any, competitor_key: str, reference: str) -> Product:
    competitor = repo.get_competitor(session, competitor_key)
    if competitor is None:
        raise SystemExit(f"unknown competitor {competitor_key}")
    product = session.scalar(
        select(Product).where(Product.competitor_id == competitor.id, Product.catalog_reference == reference)
    )
    if product is None:
        raise SystemExit(f"product {reference} not found for {competitor_key}")
    return product


def cmd_prices(args: argparse.Namespace, settings: Any) -> int:
    factory = session_factory(settings)
    with transaction(factory) as session:
        product = _product(session, args.competitor, args.product)
        print(f"{product.catalog_reference}  {product.title}")
        if args.history:
            rows = [_obs_dict(o) for o in repo.price_history(session, product.id, args.quantity)]
        elif args.quantity is not None:
            rows = repo.price_for_quantity(session, product.id, args.quantity)
        else:
            rows = repo.latest_observations(session, product.id)
        if not rows:
            print("no observed price")
        for r in rows:
            band = f"[{_n(r['min_quantity'])}, {_n(r['max_quantity']) or '∞'}{']' if r['max_inclusive'] else ')'}"
            print(
                f"  {r['observed_at']:%Y-%m-%d %H:%M:%S%z}  run={r['scrape_run_id']}"
                f"  qty={band} {r['quantity_unit'] or ''}"
                f"  {r['price']} {r['currency']} /{_per(r)}  {r['tax_mode']}"
                f"{'  promo' if r['is_promotional'] else ''}"
            )
    return 0


def cmd_product(args: argparse.Namespace, settings: Any) -> int:
    factory = session_factory(settings)
    with transaction(factory) as session:
        product = _product(session, args.competitor, args.product)
        category = session.get(Category, product.category_id) if product.category_id else None
        data = {
            "id": product.id,
            "identity_key": product.identity_key,
            "catalog_reference": product.catalog_reference,
            "external_id": product.external_id,
            "url": product.url,
            "title": product.title,
            "description": product.description,
            "long_description": (product.long_description or "")[:300] + "...",
            "brand": product.brand,
            "category": {
                "id": category.id,
                "name": category.name,
                "external_id": category.external_id,
                "url": category.url,
            }
            if category
            else None,
            "category_path": _category_path(session, category),
            "attributes": product.attributes,
            "normalized_attributes": product.normalized_attributes,
            "images": product.images,
            "extra": product.extra,
            "status": product.status,
            "first_seen_at": str(product.first_seen_at),
            "last_seen_at": str(product.last_seen_at),
            "last_price_observed_at": str(product.last_price_observed_at),
        }
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return 0


def _category_path(session: Any, category: Category | None) -> list[str]:
    path: list[str] = []
    while category is not None:
        path.insert(
            0, f"{category.name} [{category.kind}{', ' + category.external_id if category.external_id else ''}]"
        )
        category = session.get(Category, category.parent_id) if category.parent_id else None
    return path


def cmd_categories(args: argparse.Namespace, settings: Any) -> int:
    factory = session_factory(settings)
    with transaction(factory) as session:
        competitor = repo.get_competitor(session, args.competitor)
        cats = list(
            session.scalars(select(Category).where(Category.competitor_id == competitor.id).order_by(Category.id))
        )
        counts = dict(
            session.execute(
                select(Product.category_id, func.count())
                .where(Product.competitor_id == competitor.id)
                .group_by(Product.category_id)
            ).all()
        )
        children: dict[int | None, list[Category]] = {}
        for c in cats:
            children.setdefault(c.parent_id, []).append(c)

        def show(parent: int | None, depth: int) -> None:
            for c in sorted(children.get(parent, []), key=lambda c: c.name):
                n = counts.get(c.id)
                print(
                    f"{'  ' * depth}- {c.name} [{c.kind}{', ' + c.external_id if c.external_id else ''}]"
                    f"{f' ({n} products)' if n else ''}{' MISSING' if c.status == 'missing' else ''}"
                )
                show(c.id, depth + 1)

        show(None, 0)
        print(f"{len(cats)} categories, {sum(counts.values())} products")
    return 0


def cmd_runs(args: argparse.Namespace, settings: Any) -> int:
    factory = session_factory(settings)
    with transaction(factory) as session:
        if args.run:
            run = session.get(ScrapeRun, args.run)
            if run is None:
                raise SystemExit(f"run {args.run} not found")
            print(json.dumps(_run_dict(run), indent=2, default=str))
            statuses = session.execute(
                select(ScrapeRunItem.item_type, ScrapeRunItem.status, func.count())
                .where(ScrapeRunItem.scrape_run_id == run.id)
                .group_by(ScrapeRunItem.item_type, ScrapeRunItem.status)
            ).all()
            print("items:", {f"{t}/{s}": n for t, s, n in statuses})
            for item in session.scalars(
                select(ScrapeRunItem)
                .where(ScrapeRunItem.scrape_run_id == run.id, ScrapeRunItem.status.in_(["failed", "not_found"]))
                .limit(20)
            ):
                print(
                    f"  {item.status:<9} {item.item_type:<12} {item.url}\n"
                    f"            {item.error_type}: {item.error_message}"
                )
            return 0
        competitor_id = None
        if args.competitor:
            competitor_id = repo.get_competitor(session, args.competitor).id
        for run in repo.recent_runs(session, competitor_id, args.limit):
            print(
                f"#{run.id:<5} {run.operation:<17} {run.status:<9} {run.started_at:%Y-%m-%d %H:%M} "
                f"pages={run.pages_fetched}/{run.pages_failed}f cats={run.categories_seen} "
                f"products={run.products_seen} (+{run.products_created}) failed={run.products_failed} "
                f"obs={run.observations_created} credits={run.credits_used}"
            )
    return 0


def cmd_worker(args: argparse.Namespace, settings: Any) -> int:
    from competitor_catalog.services.dispatcher import Dispatcher, DispatcherConfig

    cmd_db_upgrade(args, settings)
    settings.require_scrapedo_api_key()
    dispatcher = Dispatcher(
        session_factory(settings),
        settings.competitor_config_dir,
        lambda config: build_fetcher(settings, config),
        DispatcherConfig.from_env(),
        matching=build_matching_runtime(settings),
    )
    if args.once:
        dispatcher.startup()
        dispatcher.tick()
        for thread in list(dispatcher.threads.values()):
            thread.join()
        return 0
    dispatcher.run_forever()
    return 0


def cmd_api(args: argparse.Namespace, settings: Any) -> int:
    import os

    import uvicorn

    from competitor_catalog.api.main import create_app

    cmd_db_upgrade(args, settings)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=args.host or os.environ.get("API_HOST", "0.0.0.0"),
        port=args.port or int(os.environ.get("API_PORT", "8000")),
        log_config=None,  # keep our logging configuration (secret redaction)
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def cmd_export_openapi(args: argparse.Namespace, settings: Any) -> int:
    from competitor_catalog.api.openapi import export_openapi

    export_openapi(args.out)
    print(f"wrote {args.out}")
    return 0


def cmd_fetch(args: argparse.Namespace, settings: Any) -> int:
    config = load_competitor_config(settings.competitor_config_dir, args.competitor)
    with build_fetcher(settings, config, interval=0) as client:
        page = client.fetch(args.url)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page.text, encoding="utf-8")
    print(
        f"saved {len(page.text)} chars to {args.out} (final url {page.final_url}, cost {page.meta.get('request_cost')})"
    )
    return 0


# --------------------------------------------------------------------------- helpers


def _per(r: dict[str, Any]) -> str:
    unit = r["price_unit"] or r["price_basis"]
    qty = r.get("price_unit_quantity")
    return f"{_n(qty)} {unit}" if qty not in (None, 1) else unit


def _n(value: Any) -> str:
    if value is None:
        return ""
    return format(Decimal(value).normalize(), "f")


def _obs_dict(o: Any) -> dict[str, Any]:
    return {
        "observed_at": o.observed_at,
        "scrape_run_id": o.scrape_run_id,
        "min_quantity": o.min_quantity,
        "max_quantity": o.max_quantity,
        "max_inclusive": o.max_inclusive,
        "quantity_unit": o.quantity_unit,
        "price": o.price,
        "currency": o.currency,
        "price_unit": o.price_unit,
        "price_unit_quantity": o.price_unit_quantity,
        "price_basis": o.price_basis,
        "tax_mode": o.tax_mode,
        "is_promotional": o.is_promotional,
    }


def _run_dict(run: ScrapeRun) -> dict[str, Any]:
    return {c.name: getattr(run, c.key) for c in ScrapeRun.__table__.columns}


if __name__ == "__main__":
    raise SystemExit(main())
