"""Deploy-time migration to the canonical multi-warehouse stock model."""

from __future__ import print_function

import hashlib
import math
from datetime import datetime, timezone


DEFAULT_WAREHOUSE_CODE = "udelnaya"
DEFAULT_WAREHOUSE_NAME = "Удельная"
SECONDARY_WAREHOUSE_CODE = "hong-kong"
SECONDARY_WAREHOUSE_NAME = "Гонконг"


WAREHOUSE_SQL = (
    "CREATE TABLE erp_warehouses ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT NOT NULL UNIQUE,"
    "name TEXT NOT NULL,normalized_name TEXT NOT NULL UNIQUE,"
    "is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),"
    "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
    "CREATE TABLE erp_product_warehouse_stock ("
    "product_id INTEGER NOT NULL REFERENCES catalog_excel_products(id) ON DELETE RESTRICT,"
    "warehouse_id INTEGER NOT NULL REFERENCES erp_warehouses(id) ON DELETE RESTRICT,"
    "quantity REAL NOT NULL DEFAULT 0 CHECK(quantity>=0),"
    "initialized_at TEXT,updated_at TEXT NOT NULL,"
    "PRIMARY KEY(product_id,warehouse_id))",
    "CREATE INDEX IF NOT EXISTS idx_erp_product_warehouse_stock_warehouse "
    "ON erp_product_warehouse_stock(warehouse_id,product_id)",
    "CREATE TABLE erp_user_warehouse_preferences ("
    "user_id TEXT NOT NULL,warehouse_id INTEGER NOT NULL "
    "REFERENCES erp_warehouses(id) ON DELETE CASCADE,selected_at TEXT NOT NULL,"
    "PRIMARY KEY(user_id,warehouse_id))",
    "CREATE INDEX IF NOT EXISTS idx_erp_user_warehouse_preferences_user "
    "ON erp_user_warehouse_preferences(user_id,warehouse_id)",
    "CREATE TABLE erp_stock_transfers ("
    "id TEXT PRIMARY KEY,from_warehouse_id INTEGER NOT NULL "
    "REFERENCES erp_warehouses(id) ON DELETE RESTRICT,to_warehouse_id INTEGER NOT NULL "
    "REFERENCES erp_warehouses(id) ON DELETE RESTRICT,status TEXT NOT NULL "
    "CHECK(status IN ('posted','cancelled')),idempotency_key TEXT UNIQUE,"
    "actor_id TEXT,actor_name TEXT,comment TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,"
    "cancelled_at TEXT,cancelled_by TEXT,CHECK(from_warehouse_id<>to_warehouse_id))",
    "CREATE INDEX IF NOT EXISTS idx_erp_stock_transfers_created "
    "ON erp_stock_transfers(created_at,id)",
    "CREATE TABLE erp_stock_transfer_items ("
    "transfer_id TEXT NOT NULL REFERENCES erp_stock_transfers(id) ON DELETE RESTRICT,"
    "product_id INTEGER NOT NULL REFERENCES catalog_excel_products(id) ON DELETE RESTRICT,"
    "quantity REAL NOT NULL CHECK(quantity>0),PRIMARY KEY(transfer_id,product_id))",
    "CREATE INDEX IF NOT EXISTS idx_erp_stock_transfer_items_product "
    "ON erp_stock_transfer_items(product_id,transfer_id)",
    "CREATE TABLE erp_multiwarehouse_migration_audit ("
    "id INTEGER PRIMARY KEY CHECK(id=1),default_warehouse_id INTEGER NOT NULL "
    "REFERENCES erp_warehouses(id) ON DELETE RESTRICT,product_count INTEGER NOT NULL,"
    "legacy_total REAL NOT NULL,warehouse_total REAL NOT NULL,legacy_hash TEXT NOT NULL,"
    "warehouse_hash TEXT NOT NULL,uninitialized_components INTEGER NOT NULL,"
    "verified_at TEXT NOT NULL)",
)


WAREHOUSE_COLUMNS = (
    ("catalog_stock_movements", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("catalog_stock_movements", "transfer_id", "TEXT REFERENCES erp_stock_transfers(id)"),
    ("catalog_stock_movements", "counterparty_warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("erp_sale_items", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("erp_receipts", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("erp_inventory_sessions", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("erp_writeoffs", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("catalog_excel_receipts", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("catalog_excel_receipt_operations", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("catalog_excel_receipt_rows", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("catalog_excel_manual_stock_operations", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("catalog_excel_stock_operations", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("catalog_excel_batch_rows", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
    ("erp_component_inventory_events", "warehouse_id", "INTEGER REFERENCES erp_warehouses(id)"),
)


WAREHOUSE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_catalog_stock_movements_warehouse_product "
    "ON catalog_stock_movements(warehouse_id,product_id,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_erp_sale_items_warehouse_product "
    "ON erp_sale_items(warehouse_id,product_id,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_erp_receipts_warehouse_status "
    "ON erp_receipts(warehouse_id,status,receipt_date)",
    "CREATE INDEX IF NOT EXISTS idx_erp_inventory_sessions_warehouse_status "
    "ON erp_inventory_sessions(warehouse_id,status,started_at)",
    "CREATE INDEX IF NOT EXISTS idx_erp_writeoffs_warehouse_created "
    "ON erp_writeoffs(warehouse_id,created_at,id)",
)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _columns(connection, table):
    return {row[1] for row in connection.execute(
        "PRAGMA table_info({})".format(table)
    ).fetchall()}


def _warehouse_id(connection, code):
    row = connection.execute(
        "SELECT id FROM erp_warehouses WHERE code=?", (code,)
    ).fetchone()
    if row is None:
        raise RuntimeError("warehouse migration did not create {}".format(code))
    return int(row[0])


def _next_warehouse_id(connection):
    values = []
    for row in connection.execute("SELECT id FROM erp_warehouses").fetchall():
        try:
            values.append(int(row[0]))
        except (TypeError, ValueError):
            continue
    return max(values or [0]) + 1


def _insert_warehouse(connection, code, name, timestamp, legacy_columns=False):
    row = connection.execute(
        "SELECT id FROM erp_warehouses WHERE code=?", (code,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    warehouse_id = _next_warehouse_id(connection)
    if legacy_columns:
        connection.execute(
            "INSERT INTO erp_warehouses"
            "(id,code,name,active,is_default,created_at,updated_at,normalized_name,is_active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (warehouse_id, code, name, 1, int(code == DEFAULT_WAREHOUSE_CODE),
             timestamp, timestamp, name.casefold()),
        )
    else:
        connection.execute(
            "INSERT INTO erp_warehouses"
            "(id,code,name,normalized_name,is_active,created_at,updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            (warehouse_id, code, name, name.casefold(), timestamp, timestamp),
        )
    return warehouse_id


def _upgrade_incoming_warehouse_schema(connection, tables, timestamp, ddl_observer=None):
    """Map the already released incoming-receipts warehouse model to canonical IDs."""
    if "erp_warehouses" not in tables:
        return {}, []
    columns = _columns(connection, "erp_warehouses")
    if (
        "active" not in columns
        or "is_default" not in columns
        or "is_active" in columns
    ):
        return {}, []

    for name, declaration in (
        ("normalized_name", "TEXT"),
        ("is_active", "INTEGER"),
    ):
        if name not in columns:
            statement = "ALTER TABLE erp_warehouses ADD COLUMN {} {}".format(
                name, declaration
            )
            if ddl_observer:
                ddl_observer(statement)
            connection.execute(statement)

    legacy_rows = connection.execute(
        "SELECT id,code,name,active,is_default,created_at,updated_at "
        "FROM erp_warehouses ORDER BY is_default DESC,name"
    ).fetchall()
    legacy_stock = []
    if "erp_warehouse_stocks" in tables:
        legacy_stock = connection.execute(
            "SELECT warehouse_id,product_id,quantity,updated_at FROM erp_warehouse_stocks"
        ).fetchall()

    mapping = {}
    for index, row in enumerate(legacy_rows, 1):
        old_id = str(row[0])
        legacy_code = "legacy-{}-{}".format(index, str(row[1]).casefold())
        legacy_name = "{} (legacy {})".format(row[2], index)
        connection.execute(
            "UPDATE erp_warehouses SET code=?,name=?,active=0,is_default=0,"
            "normalized_name=?,is_active=0 WHERE id=?",
            (legacy_code, legacy_name, legacy_name.casefold(), row[0]),
        )
        code = DEFAULT_WAREHOUSE_CODE if int(row[4] or 0) else str(row[1]).casefold()
        name = DEFAULT_WAREHOUSE_NAME if int(row[4] or 0) else str(row[2])
        mapping[old_id] = _insert_warehouse(
            connection, code, name, timestamp, legacy_columns=True
        )

    for table in ("erp_receipts", "catalog_stock_movements"):
        if table in tables and "warehouse_id" in _columns(connection, table):
            for old_id, new_id in mapping.items():
                connection.execute(
                    "UPDATE {} SET warehouse_id=? WHERE warehouse_id=?".format(table),
                    (new_id, old_id),
                )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_erp_warehouses_normalized_name "
        "ON erp_warehouses(normalized_name)"
    )
    return mapping, legacy_stock


def _canonical_rows(connection):
    rows = connection.execute(
        "SELECT p.id,p.stock,"
        "EXISTS(SELECT 1 FROM erp_product_bundles b WHERE b.product_id=p.id) AS is_bundle,"
        "ci.physical_stock,ci.initialized_at "
        "FROM catalog_excel_products p LEFT JOIN erp_component_inventory ci "
        "ON ci.product_id=p.id ORDER BY p.id"
    ).fetchall()
    result = []
    for row in rows:
        product_id = int(row[0])
        if int(row[2] or 0):
            continue
        is_component = row[3] is not None or row[4] is not None or connection.execute(
            "SELECT 1 FROM erp_component_inventory WHERE product_id=?", (product_id,)
        ).fetchone() is not None
        value = float((row[3] if is_component else row[1]) or 0)
        if not math.isfinite(value) or value < 0:
            raise RuntimeError(
                "multiwarehouse migration found invalid canonical stock for product {}".format(
                    product_id
                )
            )
        result.append((product_id, value, row[4] if is_component else "ordinary"))
    return result


def _digest(rows):
    payload = "\n".join(
        "{}:{:.12g}".format(int(product_id), float(quantity))
        for product_id, quantity in rows
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_multiwarehouse_migration(connection, expected=None):
    default_id = _warehouse_id(connection, DEFAULT_WAREHOUSE_CODE)
    expected = expected if expected is not None else [
        (int(row[0]), float(row[1])) for row in connection.execute(
            "SELECT product_id,quantity FROM erp_product_warehouse_stock "
            "WHERE warehouse_id=? ORDER BY product_id", (default_id,)
        ).fetchall()
    ]
    actual = [(int(row[0]), float(row[1] or 0)) for row in connection.execute(
        "SELECT p.id,COALESCE(SUM(s.quantity),0) "
        "FROM catalog_excel_products p "
        "LEFT JOIN erp_product_bundles b ON b.product_id=p.id "
        "LEFT JOIN erp_product_warehouse_stock s ON s.product_id=p.id "
        "WHERE b.product_id IS NULL GROUP BY p.id ORDER BY p.id"
    ).fetchall()]
    normalized_expected = [(int(row[0]), float(row[1])) for row in expected]
    if len(actual) != len(normalized_expected):
        raise RuntimeError("multiwarehouse migration product count mismatch")
    for before, after in zip(normalized_expected, actual):
        if before[0] != after[0] or abs(before[1] - after[1]) > 0.000001:
            raise RuntimeError(
                "multiwarehouse migration mismatch for product {}: {} != {}".format(
                    before[0], before[1], after[1]
                )
            )
    before_total = sum(row[1] for row in normalized_expected)
    after_total = sum(row[1] for row in actual)
    if abs(before_total - after_total) > 0.000001:
        raise RuntimeError(
            "multiwarehouse migration global total mismatch: {} != {}".format(
                before_total, after_total
            )
        )
    return {
        "product_count": len(actual),
        "legacy_total": before_total,
        "warehouse_total": after_total,
        "legacy_hash": _digest(normalized_expected),
        "warehouse_hash": _digest(actual),
    }


def apply_multiwarehouse_migration(connection, ddl_observer=None):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    timestamp = _now()
    legacy_mapping, legacy_stock = _upgrade_incoming_warehouse_schema(
        connection, tables, timestamp, ddl_observer
    )
    for statement in WAREHOUSE_SQL:
        table = statement.split(" ", 3)[2] if statement.startswith("CREATE TABLE ") else None
        if table and table in tables:
            continue
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)
        if table:
            tables.add(table)

    legacy_columns = "active" in _columns(connection, "erp_warehouses")
    _insert_warehouse(
        connection, DEFAULT_WAREHOUSE_CODE, DEFAULT_WAREHOUSE_NAME,
        timestamp, legacy_columns,
    )
    _insert_warehouse(
        connection, SECONDARY_WAREHOUSE_CODE, SECONDARY_WAREHOUSE_NAME,
        timestamp, legacy_columns,
    )
    default_id = _warehouse_id(connection, DEFAULT_WAREHOUSE_CODE)

    for table, column, definition in WAREHOUSE_COLUMNS:
        if table not in tables or column in _columns(connection, table):
            continue
        statement = "ALTER TABLE {} ADD COLUMN {} {}".format(
            table, column, definition
        )
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)

    already_migrated = connection.execute(
        "SELECT 1 FROM erp_multiwarehouse_migration_audit WHERE id=1"
    ).fetchone() is not None
    if already_migrated:
        expected_rows = [
            (int(row[0]), float(row[1] or 0), "canonical")
            for row in connection.execute(
                "SELECT p.id,COALESCE(SUM(s.quantity),0) "
                "FROM catalog_excel_products p "
                "LEFT JOIN erp_product_bundles b ON b.product_id=p.id "
                "LEFT JOIN erp_product_warehouse_stock s ON s.product_id=p.id "
                "WHERE b.product_id IS NULL GROUP BY p.id ORDER BY p.id"
            ).fetchall()
        ]
    else:
        expected_rows = _canonical_rows(connection)
        if legacy_mapping and legacy_stock:
            for warehouse_id, product_id, quantity, updated_at in legacy_stock:
                mapped_id = legacy_mapping.get(str(warehouse_id))
                if mapped_id is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO erp_product_warehouse_stock "
                        "(product_id,warehouse_id,quantity,initialized_at,updated_at) "
                        "VALUES (?,?,?,?,?)",
                        (product_id, mapped_id, quantity, updated_at or timestamp,
                         updated_at or timestamp),
                    )
        else:
            for product_id, quantity, initialized_at in expected_rows:
                connection.execute(
                    "INSERT OR IGNORE INTO erp_product_warehouse_stock "
                    "(product_id,warehouse_id,quantity,initialized_at,updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (product_id, default_id, quantity,
                     None if initialized_at is None else (
                         timestamp if initialized_at == "ordinary" else initialized_at
                     ), timestamp),
                )

    for table, column, _definition in WAREHOUSE_COLUMNS:
        if table in tables and column == "warehouse_id":
            connection.execute(
                "UPDATE {} SET warehouse_id=? WHERE warehouse_id IS NULL".format(table),
                (default_id,),
            )

    for statement in WAREHOUSE_INDEX_SQL:
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)

    verification = verify_multiwarehouse_migration(
        connection, [(row[0], row[1]) for row in expected_rows]
    )
    if not already_migrated:
        uninitialized = sum(row[2] is None for row in expected_rows)
        connection.execute(
            "INSERT INTO erp_multiwarehouse_migration_audit "
            "(id,default_warehouse_id,product_count,legacy_total,warehouse_total,"
            "legacy_hash,warehouse_hash,uninitialized_components,verified_at) "
            "VALUES (1,?,?,?,?,?,?,?,?)",
            (default_id, verification["product_count"], verification["legacy_total"],
             verification["warehouse_total"], verification["legacy_hash"],
             verification["warehouse_hash"], uninitialized, timestamp),
        )
    return verification
