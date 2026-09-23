#!/usr/bin/env python3
"""Regenerate the reviewed catalog schema contract from a fresh database."""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.catalog_migration_steps import apply_fresh_catalog_schema  # noqa: E402
from app.schema_migrations import (  # noqa: E402
    LEDGER_SQL,
    _json_structure,
    apply_order_comments_migration,
    apply_inventory_control_migration,
)
from app.incoming_receipts_migration import apply_incoming_receipts_migration  # noqa: E402


def main():
    target = ROOT / "app" / "catalog_schema_manifest.json"
    with tempfile.TemporaryDirectory(prefix="catalog-manifest-") as directory:
        connection = sqlite3.connect(str(Path(directory) / "catalog.db"))
        connection.row_factory = sqlite3.Row
        try:
            apply_fresh_catalog_schema(connection)
            connection.execute(LEDGER_SQL)
            connection.commit()
            apply_order_comments_migration(connection)
            connection.commit()
            apply_inventory_control_migration(connection)
            connection.commit()
            manifest = _json_structure(connection)
            apply_incoming_receipts_migration(connection)
            connection.commit()
            incoming_full = _json_structure(connection)
        finally:
            connection.close()
    target.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    incoming_tables = {
        name: incoming_full["tables"][name]
        for name in (
            "erp_receipts", "catalog_stock_movements", "erp_warehouses",
            "erp_warehouse_stocks", "erp_document_sequences",
        )
    }
    incoming = {
        "tables": incoming_tables,
        "indexes": [row for row in incoming_full["indexes"] if row[0] in incoming_tables],
        "triggers": [],
        "views": [],
    }
    (ROOT / "app" / "catalog_incoming_receipts_schema_manifest.json").write_text(
        json.dumps(incoming, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(target)


if __name__ == "__main__":
    main()
