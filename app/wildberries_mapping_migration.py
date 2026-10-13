"""Versioned local memory of explicitly selected WB variant mappings."""

WB_MAPPING_SQL = (
    "CREATE TABLE erp_wb_product_mappings ("
    "identity_key TEXT PRIMARY KEY, "
    "product_id INTEGER NOT NULL REFERENCES catalog_excel_products(id), "
    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
)


def apply_wb_mapping_migration(connection, ddl_observer=None):
    for statement in WB_MAPPING_SQL:
        if ddl_observer:
            ddl_observer(statement)
        connection.execute(statement)
