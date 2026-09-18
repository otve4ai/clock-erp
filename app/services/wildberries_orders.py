"""Normalize and persist Wildberries FBS assembly orders without side effects."""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from app.clients.wildberries_orders import WildberriesReadOnlyError


CONTENT_CACHE_KEY = "wb_content_cards_v1"
CONTENT_CACHE_TTL_SECONDS = 30 * 86400
CONTENT_NEGATIVE_CACHE_TTL_SECONDS = 86400


def _text(value):
    return str(value or "").strip()


def _price(order):
    value = order.get("convertedFinalPrice")
    if value is None:
        value = order.get("finalPrice")
    if value is None:
        value = order.get("convertedPrice")
    if value is None:
        value = order.get("price")
    try:
        return float((Decimal(str(value)) / Decimal("100")).quantize(Decimal("0.01")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _safe_image_url(value):
    value = _text(value)
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not (
            parsed.hostname == "wbbasket.ru"
            or parsed.hostname.endswith(".wbbasket.ru")
        )
    ):
        return ""
    return value


def wildberries_card_content(card):
    """Keep only display-safe, stable fields from a WB content card."""
    if not isinstance(card, dict):
        return {}
    photos = card.get("photos") if isinstance(card.get("photos"), list) else []
    image_urls = []
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        image = next((
            _safe_image_url(photo.get(key))
            for key in ("c516x688", "big", "c246x328", "square")
            if _safe_image_url(photo.get(key))
        ), "")
        if image and image not in image_urls:
            image_urls.append(image)
    return {
        "nm_id": card.get("nmID") or card.get("nmId"),
        "vendor_code": _text(card.get("vendorCode")),
        "name": _text(card.get("title")),
        "image_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
    }


def _load_content_cache(store):
    store.initialize()
    with store.connection() as connection:
        row = connection.execute(
            "SELECT value FROM orders_snapshot_meta WHERE key=?",
            (CONTENT_CACHE_KEY,),
        ).fetchone()
    if row is None:
        return {}
    try:
        cache = json.loads(row["value"])
    except (TypeError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def _save_content_cache(store, cache):
    with store.connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO orders_snapshot_meta(key,value) VALUES (?,?)",
            (CONTENT_CACHE_KEY, json.dumps(
                cache, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )),
        )


def save_wildberries_card_cache(store, cards, now=None):
    """Persist already resolved card summaries after a backup has been made."""
    current = float(time.time() if now is None else now)
    cache = _load_content_cache(store)
    for nm_id, card in cards.items():
        if isinstance(card, dict) and card:
            cache[_text(nm_id)] = {"cached_at": current, "card": card}
    _save_content_cache(store, cache)


def resolve_wildberries_cards(client, store, rows, persist_cache=True, now=None):
    """Resolve cards once per nmID and reuse the durable orders-DB cache."""
    current = float(time.time() if now is None else now)
    cache = _load_content_cache(store)
    cards = {}
    errors = []
    nm_ids = list(dict.fromkeys(
        _text(row.get("nmId") or row.get("nmID"))
        for row in rows if isinstance(row, dict)
        if _text(row.get("nmId") or row.get("nmID"))
    ))
    cache_changed = False
    get_content_card = getattr(client, "get_content_card", None)
    for nm_id in nm_ids:
        cached = cache.get(nm_id) if isinstance(cache.get(nm_id), dict) else {}
        cached_at = cached.get("cached_at")
        cached_card = cached.get("card")
        ttl = (
            CONTENT_CACHE_TTL_SECONDS
            if isinstance(cached_card, dict) and cached_card
            else CONTENT_NEGATIVE_CACHE_TTL_SECONDS
        )
        try:
            is_fresh = current - float(cached_at) < ttl
        except (TypeError, ValueError):
            is_fresh = False
        if is_fresh:
            if isinstance(cached_card, dict) and cached_card:
                cards[nm_id] = cached_card
            continue
        if not callable(get_content_card):
            continue
        try:
            resolved = wildberries_card_content(get_content_card(nm_id))
            cache[nm_id] = {"cached_at": current, "card": resolved or None}
            cache_changed = True
            if resolved:
                cards[nm_id] = resolved
        except WildberriesReadOnlyError as error:
            errors.append(error.diagnostic(stage="content_card"))
            if error.code in {
                "WB_UNAUTHORIZED", "WB_FORBIDDEN", "WB_RATE_LIMITED",
                "WB_SYNC_BUDGET",
            }:
                break
    if persist_cache and cache_changed:
        _save_content_cache(store, cache)
    return cards, errors


def normalize_wildberries_order(order, synced_at=None, content_card=None):
    """Convert one assembly task to the existing order-card contract."""
    if not isinstance(order, dict) or order.get("id") in (None, ""):
        return None
    wb_order_id = _text(order["id"])
    skus = [_text(value) for value in (order.get("skus") or []) if _text(value)]
    content = wildberries_card_content(content_card)
    if isinstance(content_card, dict) and "vendor_code" in content_card:
        content = dict(content_card)
    article = _text(
        content.get("vendor_code")
        or order.get("article")
        or order.get("supplierArticle")
        or order.get("vendorCode")
    )
    item_name = (
        _text(order.get("name"))
        or _text(content.get("name"))
        or article
        or "Товар Wildberries"
    )
    supplier_status = _text(order.get("supplierStatus")) or "new"
    wb_status = _text(order.get("wbStatus"))
    status_label = supplier_status
    if wb_status:
        status_label += " · " + wb_status
    created_at = _text(order.get("createdAt"))
    total = _price(order)
    product = {
        "source": "wildberries",
        "id": wb_order_id,
        "order_item_id": wb_order_id,
        "product_id": "",
        "name": item_name,
        "quantity": 1,
        "price": total,
        "line_total": total,
        "article": article,
        "display_article": article,
        "vendor_code": article,
        "sku": skus[0] if skus else "",
        "barcode": skus[0] if skus else "",
        "skus": skus,
        "nm_id": order.get("nmId"),
        "chrt_id": order.get("chrtId"),
        "image_url": _safe_image_url(content.get("image_url")),
        "image_urls": [
            value for value in (content.get("image_urls") or [])
            if _safe_image_url(value)
        ],
        "wb_content_enriched": bool(
            _text(content.get("name")) or _safe_image_url(content.get("image_url"))
        ),
    }
    return {
        "id": "wb:" + wb_order_id,
        "number": wb_order_id,
        "source": "wildberries",
        "source_name": "Wildberries",
        "wb_order_id": wb_order_id,
        "order_uid": _text(order.get("orderUid")),
        "rid": _text(order.get("rid")),
        "status": supplier_status,
        "status_name": status_label,
        "supplier_status": supplier_status,
        "wb_status": wb_status,
        "created_at": created_at,
        "date": created_at,
        "order_total": total,
        "price": total,
        "customer": "Покупатель Wildberries",
        "phone": "",
        "warehouse_id": order.get("warehouseId"),
        "office_id": order.get("officeId"),
        "supply_id": _text(order.get("supplyId")),
        "wb_supply_id": _text(order.get("supplyId")),
        "wb_raw": dict(order),
        "delivery_type": _text(order.get("deliveryType")),
        "article": article,
        "vendor_code": article,
        "skus": skus,
        "nm_id": order.get("nmId"),
        "chrt_id": order.get("chrtId"),
        "currency_code": order.get("convertedCurrencyCode") or order.get("currencyCode"),
        "synced_at": synced_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "products": [product],
        "item_units": 1,
        "read_only": True,
    }


def synchronize_wildberries_orders(client, store, synced_at=None):
    rows = client.get_new_orders()
    cards, content_errors = resolve_wildberries_cards(client, store, rows)
    normalized = []
    errors = 0
    for row in rows:
        order = normalize_wildberries_order(
            row,
            synced_at=synced_at,
            content_card=cards.get(_text(row.get("nmId") or row.get("nmID"))),
        )
        if order is None:
            errors += 1
        else:
            normalized.append(order)
    result = store.upsert_wildberries(normalized)
    statuses = {str(row['id']): {key: row[key] for key in ('id', 'supplierStatus', 'wbStatus') if key in row}
                for row in rows if isinstance(row, dict) and row.get('id') and row.get('supplierStatus')}
    result['statuses_updated'] = store.update_wildberries_statuses(statuses) if statuses else 0
    result.update({"received": len(rows), "errors": errors})
    if cards:
        result["content_cards"] = len(cards)
    if content_errors:
        result["content_errors"] = content_errors
    return result
