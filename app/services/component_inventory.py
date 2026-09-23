"""Transaction-local routing between legacy and explicitly confirmed physical balances."""
import math
from datetime import datetime, timezone
from app.catalog_db import CatalogDatabase
from app.services.inventory_lock import assert_products_unlocked


# Read projection only: catalog_excel_products.stock itself remains untouched.
PHYSICAL_STOCK_SQL = (
    "CASE WHEN EXISTS(SELECT 1 FROM erp_component_inventory ci WHERE ci.product_id=p.id) "
    "THEN COALESCE((SELECT physical_stock FROM erp_component_inventory ci WHERE ci.product_id=p.id),0) "
    "ELSE p.stock END"
)


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


def balance(connection, product_id, document=None, require_initialized=True):
    is_physical = physical(connection, product_id, document)
    row = connection.execute(
        "SELECT physical_stock FROM erp_component_inventory WHERE product_id=?" if is_physical
        else "SELECT stock FROM catalog_excel_products WHERE id=?", (int(product_id),),
    ).fetchone()
    if row is None:
        raise ValueError("Товар не найден.")
    if row[0] is None and require_initialized:
        raise ValueError("Физический остаток компонента не подтверждён. Установите фактическое количество.")
    return float(row[0] or 0)


def write_balance(connection, product_id, value, source, timestamp, document=None):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Физический остаток не может быть отрицательным.")
    previous = balance(connection, product_id, document, require_initialized=False)
    if physical(connection, product_id, document):
        cursor = connection.execute("UPDATE erp_component_inventory SET physical_stock=?,updated_at=? WHERE product_id=?",
                                    (value, timestamp, int(product_id)))
    else:
        cursor = connection.execute("UPDATE catalog_excel_products SET stock=?,stock_source=?,updated_at=? WHERE id=?",
                                    (value, source, timestamp, int(product_id)))
    # Operations that do not expose a warehouse selector belong to the configured
    # default warehouse. Scoped incoming documents update their warehouse row in
    # the same transaction and are deliberately excluded here.
    if source not in {"manual_receipt", "manual_receipt_cancel", "receipt", "receipt_delete"}:
        warehouse = connection.execute(
            "SELECT id FROM erp_warehouses WHERE active=1 AND is_default=1"
        ).fetchone()
        if warehouse is not None:
            row = connection.execute(
                "SELECT quantity FROM erp_warehouse_stocks WHERE warehouse_id=? AND product_id=?",
                (warehouse[0], int(product_id)),
            ).fetchone()
            before = float(row[0]) if row is not None else previous
            after = before + (value - previous)
            if after < -0.000001:
                raise ValueError("Остаток основного склада не может быть отрицательным.")
            connection.execute(
                "INSERT OR REPLACE INTO erp_warehouse_stocks "
                "(warehouse_id,product_id,quantity,updated_at) VALUES(?,?,?,?)",
                (warehouse[0], int(product_id), max(0.0, after), timestamp),
            )
    return cursor


def overlay(connection, row, product_id=None, document=None, require_initialized=True):
    if row is None:
        return None
    result = dict(row)
    pid = product_id if product_id is not None else result['id']
    result['stock'] = balance(connection, pid, document, require_initialized)
    return result


class ComponentInventory:
    def __init__(self, database=None):
        self.database = database or CatalogDatabase()

    def confirm(self, product_id, quantity, actor="", reason="Физическая инвентаризация"):
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
            row = connection.execute("SELECT physical_stock FROM erp_component_inventory WHERE product_id=?", (int(product_id),)).fetchone()
            if row is None:
                raise ValueError("Сначала сохраните товар в составе как компонент.")
            timestamp = now()
            connection.execute("UPDATE erp_component_inventory SET physical_stock=?,initialized_at=COALESCE(initialized_at,?),updated_at=? WHERE product_id=?",
                               (value,timestamp,timestamp,int(product_id)))
            connection.execute("INSERT INTO erp_component_inventory_events(product_id,stock_before,stock_after,actor,reason,created_at) VALUES (?,?,?,?,?,?)",
                               (int(product_id),row[0],value,str(actor or ''),str(reason or 'Физическая инвентаризация'),timestamp))
        return value
