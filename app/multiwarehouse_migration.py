"""Explicit migration: legacy product/component balances become TTT balances.

No runtime DDL. Existing non-default balances are deducted from the legacy
aggregate exactly once. Negative reconciliation aborts the whole transaction.
"""

MULTIWAREHOUSE_SQL = (
    "CREATE TABLE erp_stock_transfers (id TEXT PRIMARY KEY, number TEXT NOT NULL UNIQUE, "
    "from_warehouse_id TEXT NOT NULL REFERENCES erp_warehouses(id), "
    "to_warehouse_id TEXT NOT NULL REFERENCES erp_warehouses(id), "
    "status TEXT NOT NULL CHECK(status IN ('draft','in_transit','received','cancelled')), "
    "comment TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, created_at TEXT NOT NULL, "
    "sent_at TEXT, received_at TEXT, cancelled_at TEXT, idempotency_key TEXT NOT NULL UNIQUE, "
    "request_json TEXT NOT NULL, CHECK(from_warehouse_id<>to_warehouse_id))",
    "CREATE TABLE erp_stock_transfer_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "transfer_id TEXT NOT NULL REFERENCES erp_stock_transfers(id), "
    "product_id INTEGER NOT NULL REFERENCES catalog_excel_products(id), "
    "quantity REAL NOT NULL CHECK(quantity>0), UNIQUE(transfer_id,product_id))",
    "CREATE INDEX idx_transfer_items_product ON erp_stock_transfer_items(product_id,transfer_id)",
    # Keep old services/imports tied to TTT even when they write SQL directly.
    "CREATE TRIGGER trg_ttt_product_insert AFTER INSERT ON catalog_excel_products BEGIN "
    "INSERT OR REPLACE INTO erp_warehouse_stocks(warehouse_id,product_id,quantity,updated_at) "
    "VALUES('default',NEW.id,MAX(0,COALESCE(NEW.stock,0)),NEW.updated_at); END",
    "CREATE TRIGGER trg_ttt_product_update AFTER UPDATE OF stock ON catalog_excel_products "
    "WHEN NOT EXISTS(SELECT 1 FROM erp_component_inventory WHERE product_id=NEW.id) BEGIN "
    "INSERT OR REPLACE INTO erp_warehouse_stocks(warehouse_id,product_id,quantity,updated_at) "
    "VALUES('default',NEW.id,MAX(0,COALESCE(NEW.stock,0)),NEW.updated_at); END",
    "CREATE TRIGGER trg_ttt_component_insert AFTER INSERT ON erp_component_inventory BEGIN "
    "INSERT OR REPLACE INTO erp_warehouse_stocks(warehouse_id,product_id,quantity,updated_at) "
    "VALUES('default',NEW.product_id,COALESCE(NEW.physical_stock,0),NEW.updated_at); END",
    "CREATE TRIGGER trg_ttt_component_update AFTER UPDATE OF physical_stock ON erp_component_inventory BEGIN "
    "INSERT OR REPLACE INTO erp_warehouse_stocks(warehouse_id,product_id,quantity,updated_at) "
    "VALUES('default',NEW.product_id,COALESCE(NEW.physical_stock,0),NEW.updated_at); END",
    "CREATE TRIGGER trg_stock_movement_warehouse AFTER INSERT ON catalog_stock_movements "
    "WHEN NEW.warehouse_id IS NULL BEGIN UPDATE catalog_stock_movements SET warehouse_id="
    "COALESCE((SELECT warehouse_id FROM erp_receipts WHERE id=NEW.receipt_id),"
    "(SELECT warehouse_id FROM erp_sales WHERE id=NEW.sale_id),'default') WHERE id=NEW.id; END",
)


def apply_multiwarehouse_migration(connection, ddl_observer=None):
    for wid, code in (('hong-kong', 'HK'),):
        conflicts = connection.execute(
            'SELECT id,code,active FROM erp_warehouses WHERE id=? OR code=?', (wid, code)).fetchall()
        if any(row[0] != wid or row[1] != code or not row[2] for row in conflicts):
            raise ValueError('Нужно согласовать существующий склад {} перед миграцией; остатки не изменены.'.format(code))
    # The migration ledger, not this function, controls one-time data conversion.
    for table in ('erp_sales', 'erp_inventory_sessions', 'erp_writeoffs'):
        # SQLite cannot ADD a REFERENCES column with a non-NULL default to a
        # populated table while foreign keys are enabled. Backfill explicitly;
        # an insert trigger also scopes legacy writers to TTT.
        statement = "ALTER TABLE {} ADD COLUMN warehouse_id TEXT REFERENCES erp_warehouses(id)".format(table)
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)
        connection.execute("UPDATE {} SET warehouse_id='default'".format(table))
        statement = ("CREATE TRIGGER trg_{}_warehouse AFTER INSERT ON {} "
                     "WHEN NEW.warehouse_id IS NULL BEGIN UPDATE {} "
                     "SET warehouse_id='default' WHERE id=NEW.id; END").format(table, table, table)
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)
    rows = connection.execute(
        "SELECT p.id, CASE WHEN ci.product_id IS NOT NULL THEN COALESCE(ci.physical_stock,0) "
        "ELSE COALESCE(p.stock,0) END, ci.product_id, "
        "COALESCE((SELECT SUM(quantity) FROM erp_warehouse_stocks w "
        "WHERE w.product_id=p.id AND w.warehouse_id<>'default'),0) "
        "FROM catalog_excel_products p LEFT JOIN erp_component_inventory ci ON ci.product_id=p.id"
    ).fetchall()
    for product_id, total, component, other in rows:
        if float(total) - float(other) < -0.000001:
            raise ValueError('Сверка складов: общий остаток меньше других складов, товар {}'.format(product_id))
        ttt = max(0.0, float(total) - float(other))
        if other:
            if component is not None:
                connection.execute('UPDATE erp_component_inventory SET physical_stock=? WHERE product_id=?', (ttt, product_id))
            else:
                connection.execute('UPDATE catalog_excel_products SET stock=? WHERE id=?', (ttt, product_id))
        connection.execute(
            "INSERT OR REPLACE INTO erp_warehouse_stocks(warehouse_id,product_id,quantity,updated_at) "
            "VALUES('default',?,?,datetime('now'))", (product_id, ttt))
    connection.execute("UPDATE erp_warehouses SET name='Основной TTT' WHERE id='default' AND name='Основной склад'")
    for wid, code, name in (('hong-kong', 'HK', 'Гонконг'),):
        connection.execute(
            "INSERT OR IGNORE INTO erp_warehouses(id,code,name,active,is_default,created_at,updated_at) "
            "VALUES(?,?,?,1,0,datetime('now'),datetime('now'))", (wid, code, name))
    for statement in MULTIWAREHOUSE_SQL:
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)
