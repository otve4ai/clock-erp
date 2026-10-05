"""Transaction-local routing between legacy and explicitly confirmed physical balances."""
import math
import uuid
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


def document_warehouse(connection, document=None, warehouse_id=None):
    if warehouse_id is not None:
        row = connection.execute('SELECT id FROM erp_warehouses WHERE id=? AND active=1', (warehouse_id,)).fetchone()
        if row is None:
            raise ValueError('Склад не найден или отключён.')
        return row[0]
    tables = {'sale': 'erp_sales', 'receipt': 'erp_receipts',
              'inventory': 'erp_inventory_sessions', 'writeoff': 'erp_writeoffs'}
    if document and document[0] in tables:
        row = connection.execute('SELECT warehouse_id FROM {} WHERE id=?'.format(tables[document[0]]), (str(document[1]),)).fetchone()
        if row is not None and row[0]:
            return row[0]
    return 'default'


def balance(connection, product_id, document=None, require_initialized=True, warehouse_id=None):
    warehouse = document_warehouse(connection, document, warehouse_id)
    if warehouse != 'default':
        if connection.execute('SELECT 1 FROM catalog_excel_products WHERE id=?', (int(product_id),)).fetchone() is None:
            raise ValueError('Товар не найден.')
        row = connection.execute('SELECT quantity FROM erp_warehouse_stocks WHERE warehouse_id=? AND product_id=?', (warehouse, int(product_id))).fetchone()
        return float(row[0]) if row else 0.0
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


def write_balance(connection, product_id, value, source, timestamp, document=None, warehouse_id=None):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Физический остаток не может быть отрицательным.")
    warehouse = document_warehouse(connection, document, warehouse_id)
    balance(connection, product_id, document, require_initialized=False, warehouse_id=warehouse)
    if warehouse != 'default':
        return connection.execute(
            'INSERT OR REPLACE INTO erp_warehouse_stocks(warehouse_id,product_id,quantity,updated_at) VALUES(?,?,?,?)',
            (warehouse, int(product_id), value, timestamp))
    if physical(connection, product_id, document):
        cursor = connection.execute("UPDATE erp_component_inventory SET physical_stock=?,updated_at=? WHERE product_id=?",
                                    (value, timestamp, int(product_id)))
    else:
        cursor = connection.execute("UPDATE catalog_excel_products SET stock=?,stock_source=?,updated_at=? WHERE id=?",
                                    (value, source, timestamp, int(product_id)))
    # Migration-installed triggers mirror every legacy TTT writer, including
    # maintenance/import scripts. Other warehouses never enter the TTT field.
    return cursor


def overlay(connection, row, product_id=None, document=None, require_initialized=True, warehouse_id=None):
    if row is None:
        return None
    result = dict(row)
    pid = product_id if product_id is not None else result['id']
    result['stock'] = balance(connection, pid, document, require_initialized, warehouse_id=warehouse_id)
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
            delta = value - float(row[0] or 0)
            if delta:
                event_id = connection.execute('SELECT last_insert_rowid()').fetchone()[0]
                connection.execute(
                    "INSERT INTO catalog_stock_movements(id,product_id,movement_type,quantity_delta,stock_before,stock_after,source_type,source_id,source_line_id,operation_kind,source,user_name,comment,created_at,warehouse_id) "
                    "VALUES(?,?,'inventory_adjustment',?,?,?,'component_inventory',?,?,'confirm','Физическая инвентаризация',?,?,?,'default')",
                    (uuid.uuid4().hex, int(product_id), delta, float(row[0] or 0), value, str(product_id), str(event_id), str(actor or ''), str(reason or 'Физическая инвентаризация'), timestamp))
        return value
