"""Compatibility facade over the canonical warehouse stock service."""
import math
from datetime import datetime, timezone
from app.catalog_db import CatalogDatabase
from app.services.inventory_lock import assert_products_unlocked
from app.services.warehouse_stock import (
    default_warehouse_id,
    get_balance,
    set_balance,
)


# Read projection for legacy SQL callers. New code should use warehouse_stock.
PHYSICAL_STOCK_SQL = (
    "COALESCE((SELECT ws.quantity FROM erp_product_warehouse_stock ws "
    "JOIN erp_warehouses warehouse ON warehouse.id=ws.warehouse_id "
    "WHERE ws.product_id=p.id AND warehouse.code='udelnaya'),0)"
)


def warehouse_stock_sql(warehouse_id):
    return (
        "COALESCE((SELECT ws.quantity FROM erp_product_warehouse_stock ws "
        "WHERE ws.product_id=p.id AND ws.warehouse_id={}),0)"
    ).format(int(warehouse_id))


def now():
    return datetime.now(timezone.utc).isoformat()


def physical(connection, product_id, document=None):
    if document is not None:
        return connection.execute(
            "SELECT 1 FROM erp_physical_documents WHERE document_type=? AND document_id=? AND product_id=?",
            (document[0], str(document[1]), int(product_id)),
        ).fetchone() is not None
    return connection.execute("SELECT 1 FROM erp_component_inventory WHERE product_id=?", (int(product_id),)).fetchone() is not None


def remember(connection, product_id, document):
    if physical(connection, product_id):
        connection.execute("INSERT OR IGNORE INTO erp_physical_documents VALUES (?,?,?)",
                           (document[0], str(document[1]), int(product_id)))


def balance(connection, product_id, document=None, require_initialized=True,
            warehouse_id=None):
    del document
    return get_balance(
        connection, product_id, warehouse_id,
        allow_legacy_default=True,
        require_initialized=require_initialized,
    )


def write_balance(connection, product_id, value, source, timestamp, document=None,
                  warehouse_id=None):
    del source, document
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Физический остаток не может быть отрицательным.")
    if warehouse_id in (None, ""):
        warehouse_id = default_warehouse_id(connection)
    return set_balance(
        connection, product_id, warehouse_id, value, timestamp=timestamp
    )


def overlay(connection, row, product_id=None, document=None, require_initialized=True,
            warehouse_id=None):
    if row is None:
        return None
    result = dict(row)
    pid = product_id if product_id is not None else result['id']
    result['stock'] = balance(
        connection, pid, document, require_initialized, warehouse_id
    )
    return result


class ComponentInventory:
    def __init__(self, database=None):
        self.database = database or CatalogDatabase()

    def confirm(self, product_id, quantity, actor="", reason="Физическая инвентаризация",
                warehouse_id=None):
        if isinstance(quantity, bool):
            raise ValueError("Укажите целое фактическое количество от 0.")
        try:
            value = float(quantity)
        except (ValueError, TypeError):
            raise ValueError("Укажите целое фактическое количество от 0.")
        if not math.isfinite(value) or value < 0 or value != int(value):
            raise ValueError("Укажите целое фактическое количество от 0.")
        with self.database.transaction() as connection:
            assert_products_unlocked(connection, [int(product_id)], ValueError)
            row = connection.execute(
                "SELECT 1 FROM erp_component_inventory WHERE product_id=?",
                (int(product_id),),
            ).fetchone()
            if row is None:
                raise ValueError("Сначала сохраните товар в составе как компонент.")
            timestamp = now()
            if warehouse_id in (None, ""):
                warehouse_id = default_warehouse_id(connection)
            old = get_balance(
                connection, product_id, warehouse_id,
                require_initialized=False,
            )
            set_balance(connection, product_id, warehouse_id, value, timestamp)
            connection.execute("INSERT INTO erp_component_inventory_events(product_id,stock_before,stock_after,actor,reason,created_at,warehouse_id) VALUES (?,?,?,?,?,?,?)",
                               (int(product_id),old,value,str(actor or ''),str(reason or 'Физическая инвентаризация'),timestamp,warehouse_id))
        return value
