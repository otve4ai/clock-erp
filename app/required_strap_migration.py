"""Opt-in strap requirement; existing products and stock remain unchanged."""
REQUIRED_STRAP_SQL = (
    "CREATE TABLE erp_required_straps (product_id INTEGER PRIMARY KEY REFERENCES catalog_excel_products(id) ON DELETE CASCADE, updated_at TEXT NOT NULL)",
)


def apply_required_strap_migration(connection, ddl_observer=None):
    for statement in REQUIRED_STRAP_SQL:
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)
