import os
import sys
import tempfile
import types
import unittest
from itertools import product
from pathlib import Path

if os.name == "nt" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *args: None,
    )

from app.catalog_db import CatalogDatabase
from app.schema_migrations import apply_migrations
from app.services.bitrix_site_status_sync import BitrixSiteStatusSync
from app.services.excel_product_catalog import ExcelProductBatchService, ExcelProductCatalog


def excel_row(number, name, stock):
    return {
        "excel_row": number,
        "excel_name": name,
        "excel_name_raw": name,
        "excel_article": "SKU-{}".format(number),
        "excel_brand": "Test",
        "category": "Watches",
        "stock": stock,
        "stock_valid": True,
        "cell": "A-1",
        "match_status": "not_found",
        "match_method": "none",
        "confidence": 0,
        "alternatives": [],
    }


class FakeClient:
    def __init__(self, pages, fail_on_page=None):
        self.pages = pages
        self.fail_on_page = fail_on_page

    def get_products_page(self, page, limit, include_inactive=False):
        if page == self.fail_on_page:
            raise OSError("controlled failure")
        products = self.pages[page - 1]
        return {
            "products": products,
            "has_more": page < len(self.pages),
        }


class BitrixSiteStatusSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = Path(self.temp.name) / "catalog.db"
        apply_migrations(database_path, app_commit="site-status-sync-test")
        self.database = CatalogDatabase(database_path)
        ExcelProductBatchService(self.database).apply(
            [excel_row(2, "In stock", 3), excel_row(3, "No stock", 0)],
            "1" * 64,
            "products.xlsx",
        )
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM catalog_excel_products ORDER BY id"
            ).fetchall()
            self.ids = [row["id"] for row in rows]
            connection.execute(
                "UPDATE catalog_excel_products SET "
                "bitrix_external_product_id='101', bitrix_active=1 "
                "WHERE id=?", (self.ids[0],),
            )
            connection.execute(
                "UPDATE catalog_excel_products SET "
                "bitrix_external_product_id='102', bitrix_active=0 "
                "WHERE id=?", (self.ids[1],),
            )

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id, excel_name_raw, stock, cell, updated_at, "
                "bitrix_external_product_id, bitrix_active "
                "FROM catalog_excel_products ORDER BY id"
            ).fetchall()]

    def test_updates_only_site_status_after_complete_read(self):
        before = self.snapshot()
        result = BitrixSiteStatusSync(self.database, client=FakeClient([[
            {"external_product_id": "101", "active": False,
             "active_known": True},
            {"external_product_id": "102", "active": True,
             "active_known": True},
        ]])).run()
        after = self.snapshot()
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["mismatch_count"], 2)
        self.assertEqual([row["bitrix_active"] for row in after], [0, 1])
        for old, new in zip(before, after):
            for field in (
                "id", "excel_name_raw", "stock", "cell", "updated_at",
                "bitrix_external_product_id",
            ):
                self.assertEqual(old[field], new[field])

    def test_failed_later_page_preserves_every_status(self):
        before = self.snapshot()
        client = FakeClient([[
            {"external_product_id": "101", "active": False,
             "active_known": True},
        ], []], fail_on_page=2)
        with self.assertRaises(OSError):
            BitrixSiteStatusSync(self.database, client=client).run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            BitrixSiteStatusSync(self.database).summary()["outcome"], "error"
        )

    def assert_issue_counts(self, expected):
        summary = BitrixSiteStatusSync(self.database).summary()
        catalog = ExcelProductCatalog(self.database)
        for issue, count in expected.items():
            self.assertEqual(summary[issue], count)
            result = catalog.list_products(site_issue=issue, per_page=1)
            self.assertEqual(result["total"], count)
            self.assertEqual(result["stats"]["positions"], count)
            self.assertEqual(len(result["items"]), min(count, 1))
        self.assertEqual(summary["mismatch_count"], sum(expected.values()))

    def test_counts_match_list_for_hidden_batches_and_bitrix_products(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET bitrix_active = CASE "
                "WHEN stock > 0 THEN 0 ELSE 1 END"
            )
        expected = {"in_stock_inactive": 1, "out_of_stock_active": 1}
        self.assert_issue_counts(expected)
        with self.database.transaction() as connection:
            connection.execute("UPDATE catalog_excel_batches SET status='superseded'")
        self.assert_issue_counts(dict.fromkeys(expected, 0))
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET source_key='bitrix:' || id"
            )
        self.assert_issue_counts(expected)
        with self.database.transaction() as connection:
            connection.execute("UPDATE catalog_excel_products SET active=0")
        self.assert_issue_counts(dict.fromkeys(expected, 0))

    def test_counts_match_list_during_and_after_inventory(self):
        # Reproduce the reported 39 vs 36 mismatch on an isolated database.
        ExcelProductBatchService(self.database).apply(
            [excel_row(number, "No stock {}".format(number), 0)
             for number in range(10, 49)],
            "2" * 64,
            "inventory-count.xlsx",
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET bitrix_active=1, "
                "bitrix_external_product_id='linked-' || id WHERE active=1"
            )
            rows = connection.execute(
                "SELECT id, brand_id FROM catalog_excel_products "
                "WHERE active=1 ORDER BY id LIMIT 3"
            ).fetchall()
        expected = {"in_stock_inactive": 0, "out_of_stock_active": 39}
        self.assert_issue_counts(expected)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO erp_inventory_sessions "
                "(id, brand_id, status, started_at, updated_at) "
                "VALUES ('count-test', ?, 'active', '2026-09-30', '2026-09-30')",
                (rows[0]["brand_id"],),
            )
            for row in rows:
                product_id = row["id"]
                connection.execute(
                    "INSERT INTO erp_inventory_items "
                    "(id, session_id, product_id, snapshot_stock, status, "
                    "appearance, snapshot_at) "
                    "VALUES (?, 'count-test', ?, 0, 'pending', 'snapshot', '2026-09-30')",
                    (str(product_id), product_id),
                )
        self.assert_issue_counts({"in_stock_inactive": 0, "out_of_stock_active": 36})
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE erp_inventory_sessions SET status='completed' WHERE id='count-test'"
            )
        self.assert_issue_counts(expected)

    def test_counts_match_list_across_stock_link_and_status_combinations(self):
        for stock, active, linked in product((0, 3), (None, 0, 1), (None, " ", "101")):
            with self.subTest(stock=stock, active=active, linked=linked):
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE catalog_excel_products SET stock=?, "
                        "bitrix_active=?, bitrix_external_product_id=?",
                        (stock, active, linked),
                    )
                has_link = bool(str(linked or "").strip())
                self.assert_issue_counts({
                    "in_stock_inactive": 2 if stock > 0 and active == 0 and has_link else 0,
                    "out_of_stock_active": 2 if stock == 0 and active == 1 and has_link else 0,
                })

    def test_unknown_status_is_not_guessed(self):
        result = BitrixSiteStatusSync(
            self.database,
            client=FakeClient([[
                {"external_product_id": "101", "active": True,
                 "active_known": False},
            ]]),
        ).run()
        self.assertEqual([row["bitrix_active"] for row in self.snapshot()], [None, None])
        self.assertEqual(result["unknown_source_statuses"], 1)
        self.assertEqual(result["missing_from_bitrix"], 1)

    def test_missing_product_clears_old_active_and_keeps_link_and_stock(self):
        before = self.snapshot()
        service = BitrixSiteStatusSync(self.database, client=FakeClient([[
            {"external_product_id": "102", "active": False},
        ]]))
        report = service.run()
        after = self.snapshot()
        self.assertIsNone(after[0]["bitrix_active"])
        self.assertEqual(after[0], {**before[0], "bitrix_active": None})
        self.assertEqual(report["missing_from_bitrix"], 1)
        self.assertEqual(report["unknown_statuses"], 1)
        service.client = FakeClient([[
            {"external_product_id": "101", "active": False},
            {"external_product_id": "102", "active": False},
        ]])
        service.run()
        self.assertEqual(self.snapshot()[0]["bitrix_active"], 0)

    def test_empty_intermediate_page_fails_without_clearing_statuses(self):
        before = self.snapshot()
        service = BitrixSiteStatusSync(self.database, client=FakeClient([[], []]))
        with self.assertRaises(ValueError):
            service.run()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(service.summary()["stale"])

    def test_incomplete_total_fails_without_clearing_statuses(self):
        before = self.snapshot()
        client = types.SimpleNamespace(get_products_page=lambda **kwargs: {
            "products": [{"external_product_id": "102", "active": True}],
            "has_more": False, "total": 2,
        })
        with self.assertRaises(ValueError):
            BitrixSiteStatusSync(self.database, client=client).run()
        self.assertEqual(self.snapshot(), before)

    def test_interrupted_run_is_error_and_preserves_last_success(self):
        service = BitrixSiteStatusSync(self.database, client=FakeClient([[]]))
        previous = service.run()["last_success_at"]
        service._create_run()
        summary = service.summary()
        self.assertEqual(summary["outcome"], "error")
        self.assertEqual(summary["last_error"], "InterruptedSync")
        self.assertEqual(summary["last_success_at"], previous)
        self.assertTrue(summary["stale"])

    def test_running_worker_with_busy_lock_is_not_marked_interrupted(self):
        service = BitrixSiteStatusSync(self.database)
        service._create_run()
        service.lock = types.SimpleNamespace(handle=None, acquire=lambda: False)
        self.assertEqual(service.summary()["outcome"], "running")

    def test_indicator_count_excludes_unlinked_and_unknown(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET bitrix_active=0 WHERE id=?",
                (self.ids[0],),
            )
            connection.execute(
                "UPDATE catalog_excel_products SET "
                "bitrix_external_product_id=NULL, bitrix_active=NULL WHERE id=?",
                (self.ids[1],),
            )
        summary = BitrixSiteStatusSync(self.database).summary()
        self.assertEqual(summary["in_stock_inactive"], 1)
        self.assertEqual(summary["in_stock_unlinked"], 0)
        self.assertEqual(summary["mismatch_count"], 1)


if __name__ == "__main__":
    unittest.main()
