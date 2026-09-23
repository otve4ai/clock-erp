"""Add explicit incoming document types and warehouse-scoped balances."""

import json


INCOMING_RECEIPTS_SQL = (
    "CREATE TABLE erp_warehouses (id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, "
    "name TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), "
    "is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX idx_erp_warehouses_default ON erp_warehouses(is_default,active)",
    "CREATE TABLE erp_warehouse_stocks (warehouse_id TEXT NOT NULL "
    "REFERENCES erp_warehouses(id) ON DELETE RESTRICT, product_id INTEGER NOT NULL "
    "REFERENCES catalog_excel_products(id) ON DELETE RESTRICT, quantity REAL NOT NULL "
    "DEFAULT 0 CHECK(quantity>=0), updated_at TEXT NOT NULL, "
    "PRIMARY KEY(warehouse_id,product_id))",
    "CREATE INDEX idx_erp_warehouse_stocks_product "
    "ON erp_warehouse_stocks(product_id,warehouse_id)",
    "CREATE TABLE erp_document_sequences (document_type TEXT PRIMARY KEY, "
    "last_value INTEGER NOT NULL CHECK(last_value>=0))",
)
INCOMING_RECEIPTS_DEFINITION = INCOMING_RECEIPTS_SQL + (
    "columns-v1;default-warehouse-physical-balance-backfill-v1;"
    "explicit-supply-and-sale-cancellation-backfill-v1;legacy-unclassified-v1",
)


def _columns(connection, table):
    return {row[1] for row in connection.execute("PRAGMA table_info({})".format(table))}


def apply_incoming_receipts_migration(connection, ddl_observer=None):
    for statement in INCOMING_RECEIPTS_SQL:
        tokens = statement.split()
        object_type = tokens[1].lower()
        object_name = tokens[2]
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
            (object_type, object_name),
        ).fetchone() is not None:
            continue
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)
    additions = {
        "erp_receipts": (
            ("operation_type", "TEXT"),
            ("warehouse_id", "TEXT REFERENCES erp_warehouses(id) ON DELETE RESTRICT"),
            ("reason_code", "TEXT"),
            ("posted_at", "TEXT"),
            ("posted_by", "TEXT"),
        ),
        "catalog_stock_movements": (
            ("warehouse_id", "TEXT REFERENCES erp_warehouses(id) ON DELETE RESTRICT"),
        ),
    }
    for table, definitions in additions.items():
        existing = _columns(connection, table)
        for name, declaration in definitions:
            if name not in existing:
                statement = "ALTER TABLE {} ADD COLUMN {} {}".format(table, name, declaration)
                if ddl_observer:
                    ddl_observer(statement)
                connection.execute(statement)
    now = "1970-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT OR IGNORE INTO erp_warehouses "
        "(id,code,name,active,is_default,created_at,updated_at) "
        "VALUES ('default','MAIN','Основной склад',1,1,?,?)", (now, now)
    )
    connection.execute(
        "INSERT OR IGNORE INTO erp_warehouse_stocks "
        "(warehouse_id,product_id,quantity,updated_at) "
        "SELECT 'default',p.id,CASE WHEN EXISTS("
        "SELECT 1 FROM erp_component_inventory ci WHERE ci.product_id=p.id) "
        "THEN COALESCE((SELECT physical_stock FROM erp_component_inventory ci "
        "WHERE ci.product_id=p.id),0) ELSE COALESCE(p.stock,0) END,? "
        "FROM catalog_excel_products p", (now,)
    )
    connection.execute("UPDATE erp_receipts SET warehouse_id='default' WHERE warehouse_id IS NULL")
    connection.execute(
        "UPDATE catalog_stock_movements SET warehouse_id='default' WHERE warehouse_id IS NULL"
    )
    rows = connection.execute(
        "SELECT id,metadata_json FROM erp_receipts WHERE operation_type IS NULL"
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row[1] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        operation_type = None
        if isinstance(metadata, dict) and metadata.get("source_type") == "supply":
            operation_type = "supply"
        elif str(row[0]).startswith("sale-cancellation:") or (
            isinstance(metadata, dict) and metadata.get("automatic_type") == "sale_cancellation"
        ):
            operation_type = "sale_cancellation"
        if operation_type:
            connection.execute(
                "UPDATE erp_receipts SET operation_type=? WHERE id=?", (operation_type, row[0])
            )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_erp_receipts_operation_date ON erp_receipts(operation_type,receipt_date,id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_erp_receipts_warehouse_date ON erp_receipts(warehouse_id,receipt_date,id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_catalog_stock_movements_warehouse ON catalog_stock_movements(warehouse_id,product_id,created_at)")
