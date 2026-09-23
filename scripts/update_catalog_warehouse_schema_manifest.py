#!/usr/bin/env python3
"""Regenerate the additive multiwarehouse schema contract from a fresh DB."""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.multiwarehouse_migration import WAREHOUSE_COLUMNS
from app.schema_migrations import apply_migrations, schema_structure


TARGET = ROOT / "app" / "catalog_warehouse_schema_manifest.json"
NEW_TABLES = {
    "erp_warehouses",
    "erp_product_warehouse_stock",
    "erp_user_warehouse_preferences",
    "erp_stock_transfers",
    "erp_stock_transfer_items",
    "erp_multiwarehouse_migration_audit",
}
TOUCHED_TABLES = NEW_TABLES | {item[0] for item in WAREHOUSE_COLUMNS}
INDEX_PREFIXES = (
    "idx_erp_product_warehouse_stock_",
    "idx_erp_user_warehouse_preferences_",
    "idx_erp_stock_transfers_",
    "idx_erp_stock_transfer_items_",
    "idx_catalog_stock_movements_warehouse_",
    "idx_erp_sale_items_warehouse_",
    "idx_erp_receipts_warehouse_",
    "idx_erp_inventory_sessions_warehouse_",
    "idx_erp_writeoffs_warehouse_",
)


def main():
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "catalog.db"
        try:
            apply_migrations(database, app_commit="manifest")
        except Exception as error:
            if "catalog schema mismatch" not in str(error):
                raise
        with sqlite3.connect(str(database)) as connection:
            structure = schema_structure(connection)
    payload = {
        "tables": {
            name: structure["tables"][name]
            for name in sorted(TOUCHED_TABLES)
            if name in structure["tables"]
        },
        "indexes": [
            item for item in structure["indexes"]
            if item[0] in NEW_TABLES or item[1].startswith(INDEX_PREFIXES)
        ],
        "triggers": [],
        "views": [],
    }
    TARGET.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
