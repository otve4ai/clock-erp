#!/usr/bin/env python3
"""Verify a migrated catalog copy against its untouched source database."""

import argparse
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.multiwarehouse_migration import _canonical_rows, _digest
from app.services.excel_product_catalog import warehouse_stock_cte


HISTORICAL_TABLES = (
    "catalog_stock_movements",
    "erp_sale_items",
    "erp_receipts",
    "erp_inventory_sessions",
    "erp_writeoffs",
    "catalog_excel_receipts",
    "catalog_excel_receipt_operations",
    "catalog_excel_receipt_rows",
    "catalog_excel_manual_stock_operations",
    "catalog_excel_stock_operations",
    "catalog_excel_batch_rows",
    "erp_component_inventory_events",
)


def connect(path):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def scalar(connection, sql, parameters=()):
    return connection.execute(sql, parameters).fetchone()[0]


def timed(connection, sql, parameters=(), repeats=12):
    samples = []
    rows = 0
    for _ in range(repeats):
        started = time.perf_counter()
        result = connection.execute(sql, parameters).fetchall()
        samples.append((time.perf_counter() - started) * 1000)
        rows = len(result)
    ordered = sorted(samples)
    return {
        "rows": rows,
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max_ms": round(max(samples), 3),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--migrated", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    with connect(arguments.original) as original, connect(arguments.migrated) as migrated:
        expected_rows = [(int(row[0]), float(row[1])) for row in _canonical_rows(original)]
        warehouses = {
            row["code"]: dict(row)
            for row in migrated.execute(
                "SELECT id,code,name,is_active FROM erp_warehouses ORDER BY id"
            ).fetchall()
        }
        udelnaya_id = int(warehouses["udelnaya"]["id"])
        hong_kong_id = int(warehouses["hong-kong"]["id"])
        actual_rows = [
            (int(row[0]), float(row[1]))
            for row in migrated.execute(
                "SELECT p.id,COALESCE(s.quantity,0) "
                "FROM catalog_excel_products p "
                "LEFT JOIN erp_product_bundles b ON b.product_id=p.id "
                "LEFT JOIN erp_product_warehouse_stock s "
                "ON s.product_id=p.id AND s.warehouse_id=? "
                "WHERE b.product_id IS NULL ORDER BY p.id",
                (udelnaya_id,),
            ).fetchall()
        ]
        expected = dict(expected_rows)
        actual = dict(actual_rows)
        mismatches = [
            {"product_id": product_id, "old": quantity,
             "new_udelnaya": actual.get(product_id)}
            for product_id, quantity in expected_rows
            if product_id not in actual
            or abs(quantity - actual[product_id]) > 0.000001
        ]
        unexpected = sorted(set(actual) - set(expected))
        history = {}
        for table in HISTORICAL_TABLES:
            columns = {
                row[1] for row in migrated.execute(
                    "PRAGMA table_info({})".format(table)
                ).fetchall()
            }
            if "warehouse_id" not in columns:
                history[table] = {"error": "warehouse_id missing"}
                continue
            history[table] = {
                "rows": int(scalar(migrated, "SELECT COUNT(*) FROM " + table)),
                "backfilled": int(scalar(
                    migrated, "SELECT COUNT(*) FROM " + table
                    + " WHERE warehouse_id IS NOT NULL"
                )),
                "missing": int(scalar(
                    migrated, "SELECT COUNT(*) FROM " + table
                    + " WHERE warehouse_id IS NULL"
                )),
            }
        stock_cte, stock_parameters = warehouse_stock_cte([udelnaya_id, hong_kong_id])
        benchmark_queries = {
            "product_list": (
                stock_cte + "SELECT p.id,s.quantity FROM catalog_excel_products p "
                "JOIN warehouse_stock_view s ON s.product_id=p.id "
                "WHERE p.active=1 ORDER BY p.id DESC LIMIT 100",
                stock_parameters,
            ),
            "in_stock": (
                stock_cte + "SELECT p.id FROM catalog_excel_products p "
                "JOIN warehouse_stock_view s ON s.product_id=p.id "
                "WHERE p.active=1 AND s.quantity>0 ORDER BY p.id DESC LIMIT 100",
                stock_parameters,
            ),
            "out_of_stock": (
                stock_cte + "SELECT p.id FROM catalog_excel_products p "
                "JOIN warehouse_stock_view s ON s.product_id=p.id "
                "WHERE p.active=1 AND s.quantity<=0 ORDER BY p.id DESC LIMIT 100",
                stock_parameters,
            ),
            "search": (
                stock_cte + "SELECT p.id,s.quantity FROM catalog_excel_products p "
                "JOIN warehouse_stock_view s ON s.product_id=p.id "
                "WHERE p.active=1 AND (p.excel_name_raw LIKE ? OR p.excel_article LIKE ?) "
                "ORDER BY p.id DESC LIMIT 100",
                stock_parameters + ["%Rolex%", "%Rolex%"],
            ),
            "pagination": (
                stock_cte + "SELECT p.id,s.quantity FROM catalog_excel_products p "
                "JOIN warehouse_stock_view s ON s.product_id=p.id "
                "WHERE p.active=1 ORDER BY p.id DESC LIMIT 100 OFFSET 400",
                stock_parameters,
            ),
            "warehouse_totals": (
                "SELECT w.id,COALESCE(SUM(s.quantity),0) FROM erp_warehouses w "
                "LEFT JOIN erp_product_warehouse_stock s ON s.warehouse_id=w.id "
                "WHERE w.is_active=1 GROUP BY w.id ORDER BY w.id",
                [],
            ),
            "bundle_calculation": (
                "SELECT b.product_id,MIN(CAST(COALESCE(s.quantity,0)/c.quantity AS INTEGER)) "
                "FROM erp_product_bundles b JOIN erp_bundle_components c "
                "ON c.product_id=b.product_id LEFT JOIN erp_product_warehouse_stock s "
                "ON s.product_id=c.component_id AND s.warehouse_id=? "
                "GROUP BY b.product_id",
                [udelnaya_id],
            ),
        }
        result = {
            "status": "passed" if not mismatches and not unexpected else "failed",
            "counts": {
                "products": int(scalar(original, "SELECT COUNT(*) FROM catalog_excel_products")),
                "components": int(scalar(original, "SELECT COUNT(*) FROM erp_component_inventory")),
                "bundles": int(scalar(original, "SELECT COUNT(*) FROM erp_product_bundles")),
                "warehouse_stock_rows": int(scalar(migrated, "SELECT COUNT(*) FROM erp_product_warehouse_stock")),
            },
            "stock_reconciliation": {
                "old_total": sum(row[1] for row in expected_rows),
                "new_udelnaya_total": sum(row[1] for row in actual_rows),
                "hong_kong_total": float(scalar(
                    migrated,
                    "SELECT COALESCE(SUM(quantity),0) FROM erp_product_warehouse_stock WHERE warehouse_id=?",
                    (hong_kong_id,),
                )),
                "products_compared": len(expected_rows),
                "mismatch_count": len(mismatches),
                "unexpected_product_count": len(unexpected),
                "mismatches": mismatches[:20],
                "unexpected_product_ids": unexpected[:20],
                "old_hash": _digest(expected_rows),
                "new_udelnaya_hash": _digest(actual_rows),
            },
            "migration_audit": dict(migrated.execute(
                "SELECT * FROM erp_multiwarehouse_migration_audit WHERE id=1"
            ).fetchone()),
            "historical_backfill": history,
            "integrity": {
                "quick_check": [row[0] for row in migrated.execute("PRAGMA quick_check")],
                "foreign_key_violations": len(migrated.execute("PRAGMA foreign_key_check").fetchall()),
            },
            "performance": {
                name: timed(migrated, sql, parameters)
                for name, (sql, parameters) in benchmark_queries.items()
            },
        }
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output:
        arguments.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
