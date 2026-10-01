"""Bounded read-only CDEK sync against the existing local orders snapshot."""
import json
import os
import sys
from pathlib import Path

from app.clients.cdek import CdekError
from app.services.cdek_delivery import CdekDelivery
from app.services.orders_snapshot import OrdersSnapshotStore
from app.services.cdek_sales import group_sales


def main():
    # Use the same canonical sales read model as the screen: it includes manual
    # sales, warehouse operations and tracking overrides without external I/O.
    from app.web import api_sales_records
    shipments = [item for item in group_sales(api_sales_records()) if item["tracking"] or item["number"]]
    store = OrdersSnapshotStore().initialize()
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM orders_snapshot WHERE source='tictactoy' ORDER BY created_sort DESC"
        ).fetchall()
    # Respect manually corrected waybills, just like the order card.
    override_path = Path(os.getenv("CDEK_ORDER_OVERRIDES_PATH") or "instance/order_overrides.json")
    overrides = json.loads(override_path.read_text(encoding="utf-8")) if override_path.exists() else {}
    if not isinstance(overrides, dict):
        raise ValueError("Invalid order overrides; refusing CDEK sync")
    orders = [json.loads(row[0]) for row in rows]
    for order in orders:
        override = overrides.get(str(order.get("id") or order.get("ID")))
        if isinstance(override, dict) and "tracking" in override:
            order["tracking"] = str(override["tracking"] or "")
            order["track_number"] = order["tracking"]
    try:
        # Sales use one cache per waybill; order-card caches remain compatible.
        result = CdekDelivery().sync_pending(shipments + orders)
    except CdekError as error:
        print(json.dumps({"error": error.code, "message": str(error)}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
