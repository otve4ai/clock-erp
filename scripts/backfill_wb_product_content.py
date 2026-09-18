#!/usr/bin/env python3
"""Preview or safely apply WB order product title/photo enrichment."""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.clients.wildberries_orders import WildberriesOrdersReadOnlyClient
from app.services.orders_snapshot import OrdersSnapshotStore
from app.services.wildberries_orders import (
    normalize_wildberries_order,
    resolve_wildberries_cards,
    save_wildberries_card_cache,
)


def load_orders(store):
    store.initialize()
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM orders_snapshot "
            "WHERE source='wildberries' ORDER BY external_order_id"
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def raw_order(order):
    raw = order.get("wb_raw")
    if isinstance(raw, dict) and raw.get("id") not in (None, ""):
        return raw
    product = (order.get("products") or [{}])[0]
    return {
        "id": order.get("wb_order_id") or order.get("number"),
        "article": product.get("article") or order.get("article"),
        "nmId": product.get("nm_id") or order.get("nm_id"),
        "chrtId": product.get("chrt_id") or order.get("chrt_id"),
        "skus": product.get("skus") or order.get("skus") or [],
        "createdAt": order.get("created_at"),
        "convertedFinalPrice": (
            int(round(float(order.get("order_total")) * 100))
            if order.get("order_total") is not None else None
        ),
        "supplierStatus": order.get("supplier_status") or order.get("status"),
        "wbStatus": order.get("wb_status"),
    }


def build_preview(orders, cards):
    result = {
        "found": len(orders),
        "new_names": 0,
        "new_photos": 0,
        "display_articles_changed": 0,
        "not_enriched": 0,
    }
    normalized = []
    for order in orders:
        raw = raw_order(order)
        nm_id = str(raw.get("nmId") or raw.get("nmID") or "").strip()
        card = cards.get(nm_id)
        enriched = normalize_wildberries_order(raw, content_card=card)
        if not enriched:
            result["not_enriched"] += 1
            continue
        old = (order.get("products") or [{}])[0]
        new = enriched["products"][0]
        old_name = str(old.get("name") or "").strip()
        old_photo = str(old.get("image_url") or "").strip()
        old_display_article = str(
            old.get("display_article") or old.get("sku") or old.get("article") or ""
        ).strip()
        if new.get("name") and new["name"] != old_name:
            result["new_names"] += 1
        if new.get("image_url") and not old_photo:
            result["new_photos"] += 1
        if new.get("display_article") != old_display_article:
            result["display_articles_changed"] += 1
        if not card or not (new.get("name") or new.get("image_url")):
            result["not_enriched"] += 1
        normalized.append(enriched)
    return result, normalized


def sqlite_backup(source_path, backup_dir):
    directory = Path(backup_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / "orders-before-wb-content-{}.db".format(stamp)
    source = sqlite3.connect(str(Path(source_path).resolve()))
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="instance/orders.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir")
    arguments = parser.parse_args()
    if arguments.apply and not arguments.backup_dir:
        parser.error("--apply requires --backup-dir")

    store = OrdersSnapshotStore(arguments.database)
    orders = load_orders(store)
    rows = [raw_order(order) for order in orders]
    client = WildberriesOrdersReadOnlyClient(os.getenv("WB_API_TOKEN"))
    cards, errors = resolve_wildberries_cards(
        client, store, rows, persist_cache=False
    )
    preview, normalized = build_preview(orders, cards)
    preview["content_errors"] = len(errors)
    preview["mode"] = "apply" if arguments.apply else "preview"
    preview["applied"] = 0
    preview["backup"] = ""

    if arguments.apply:
        backup = sqlite_backup(store.path, arguments.backup_dir)
        save_wildberries_card_cache(store, cards)
        outcome = store.upsert_wildberries(normalized)
        preview["applied"] = outcome["updated"]
        preview["backup"] = str(backup)

    print(json.dumps(preview, ensure_ascii=False, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
