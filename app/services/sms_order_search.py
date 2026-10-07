"""Read-only order-number search for the SMS composer."""

import json

from app.services.orders_snapshot import normalize_exact_order_number_query


def search_orders_by_number(store, query, limit=20):
    """Bounded number-only lookup, with exact display numbers first."""
    query = normalize_exact_order_number_query(query) or str(query or "").strip()
    if not query:
        return []
    store.initialize()
    folded = query.casefold()
    pattern = "%" + folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT order_id, payload_json FROM orders_snapshot "
            "WHERE number_fold LIKE ? ESCAPE '\\' OR order_id = ? OR external_order_id = ? "
            "ORDER BY CASE WHEN number_fold = ? THEN 0 ELSE 1 END, order_id DESC LIMIT ?",
            (pattern, query, query, folded, min(50, max(1, int(limit)))),
        ).fetchall()
    return [dict(json.loads(row["payload_json"]), id=row["order_id"]) for row in rows]
