"""Bounded read-only CDEK sync against the existing local orders snapshot."""
import json
import os
import re
import sys
from pathlib import Path

from app.clients.cdek import CdekError
from app.services.cdek_delivery import CdekDelivery
from app.services.cdek_sales import group_sales
from app.services.cdek_sync import CdekSync


def tracked_cards(delivery, load_order):
    """Only revisit cards explicitly checked before; never enqueue the archive."""
    orders = []
    for path in delivery.path.glob("*.json"):
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise CdekError("CDEK_CACHE", "Не удалось прочитать сохранённые данные СДЭК.") from None
        if not isinstance(cached, dict):
            raise CdekError("CDEK_CACHE", "Повреждены сохранённые данные СДЭК.")
        order_id = str(cached.get("order_id") or "")
        # Sales cache identifiers are 64-character hashes, not snapshot IDs.
        if not re.fullmatch(r"[0-9]{1,20}", order_id):
            continue
        if not delivery.read(order_id):
            continue
        order = load_order(order_id)
        if order and order.get("source") in (None, "", "tictactoy"):
            orders.append(order)
    return orders


def main():
    # Use the same canonical sales read model as the screen: it includes manual
    # sales, warehouse operations and tracking overrides without external I/O.
    from app.web import api_sales_records
    from app.services.orders_snapshot import OrdersSnapshotStore
    shipments = [item for item in group_sales(api_sales_records()) if item["tracking"] or item["number"]]
    store = OrdersSnapshotStore().initialize()
    delivery = CdekDelivery()
    try:
        orders = tracked_cards(delivery, store.get)
    except CdekError as error:
        print(json.dumps({"error": error.code, "message": str(error)}, ensure_ascii=True))
        return 1
    # Respect manually corrected waybills, just like the order card.
    override_path = Path(os.getenv("CDEK_ORDER_OVERRIDES_PATH") or "instance/order_overrides.json")
    overrides = json.loads(override_path.read_text(encoding="utf-8")) if override_path.exists() else {}
    if not isinstance(overrides, dict):
        raise ValueError("Invalid order overrides; refusing CDEK sync")
    for order in orders:
        override = overrides.get(str(order.get("id") or order.get("ID")))
        if isinstance(override, dict) and "tracking" in override:
            order["tracking"] = str(override["tracking"] or "")
            order["track_number"] = order["tracking"]
    try:
        # Sales use one cache per waybill; order-card caches remain compatible.
        result = CdekSync(delivery).run(lambda: shipments + orders)
    except CdekError as error:
        print(json.dumps({"error": error.code, "message": str(error)}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
