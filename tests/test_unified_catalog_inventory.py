import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from app.catalog_db import CatalogDatabase
from app.remove_product_collections_migration import apply_remove_collections_migration
from app.incoming_receipts_migration import apply_incoming_receipts_migration
from app.catalog_migration_steps import apply_fresh_catalog_schema
from app.services.excel_product_catalog import (
    ExcelProductBatchService,
    ExcelProductCatalog,
)
from app.services.receipt_inventory import ReceiptInventory
from app.services.sales_inventory import SalesInventory
from app.services.shared_catalog import (
    DuplicateCatalogValueError,
    SharedCatalog,
)
from scripts.migrate_unified_catalog import (
    audit_legacy_links,
    backup_database,
    migrate,
    migration_applied,
    persist_legacy_audit,
)


class UnifiedCatalogInventoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = CatalogDatabase(
            Path(self.temp.name) / "catalog.db",
            cache_initialization=False,
        )
        self.database.initialize()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog_excel_batches ("
                "id, file_sha256, source_filename, row_count, total_stock, "
                "positive_rows, zero_rows, status, created_at, applied_at"
                ") VALUES ('unified-test', 'unified-sha', 'test.xlsx', "
                "0, 0, 0, 0, 'active', ?, ?)",
                ("2026-07-30T08:00:00+00:00", "2026-07-30T08:00:00+00:00"),
            )
        self.products = ExcelProductCatalog(self.database)
        self.catalog = SharedCatalog(self.database)
        self.receipts = ReceiptInventory(self.database)
        self.sales = SalesInventory(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def create_product(
        self,
        name="Casio A168",
        brand="Casio",
        category="Наручные часы",
        stock=0,
    ):
        return self.products.create_product(
            name=name,
            article=name.replace(" ", "-").upper(),
            brand=brand,
            category=category,
            stock=stock,
        )

    def stock(self, product_id):
        return self.catalog.get_product(
            product_id,
            include_archived=True,
        )["stock"]

    def test_shared_taxonomy_normalizes_case_and_cascades_by_ids(self):
        casio = self.create_product()
        casio_variant = self.create_product(
            name="Casio F-91W",
            brand="  CASIO  ",
            category="  наручные   ЧАСЫ  ",
        )
        seiko = self.create_product(
            name="Seiko 5",
            brand="Seiko",
            category="Механические часы",
        )

        with self.assertRaises(DuplicateCatalogValueError) as duplicate:
            self.catalog.create_brand("  cASIo  ")
        self.assertEqual(
            duplicate.exception.existing["id"],
            casio["brand_id"],
        )
        self.assertEqual(casio_variant["brand_id"], casio["brand_id"])
        self.assertEqual(casio_variant["category_id"], casio["category_id"])
        with self.assertRaises(DuplicateCatalogValueError) as alias:
            self.catalog.create_brand("Касио")
        self.assertEqual(alias.exception.existing["id"], casio["brand_id"])

        casio_categories = self.catalog.list_categories(
            brand_id=casio["brand_id"],
        )
        self.assertEqual(
            [item["id"] for item in casio_categories],
            [casio["category_id"]],
        )
        self.assertNotIn(
            seiko["category_id"],
            [item["id"] for item in casio_categories],
        )
        products = self.catalog.list_products(
            brand_id=casio["brand_id"],
            category_id=casio["category_id"],
        )
        self.assertEqual(
            {item["id"] for item in products},
            {str(casio["id"]), str(casio_variant["id"])},
        )

    def test_category_ids_are_global_across_brands(self):
        casio = self.create_product()
        seiko = self.create_product(
            name="Seiko Global Category",
            brand="Seiko",
            category="  нАРУЧНЫЕ ЧАСЫ  ",
        )

        self.assertNotEqual(casio["brand_id"], seiko["brand_id"])
        self.assertEqual(casio["category_id"], seiko["category_id"])
        with self.assertRaises(DuplicateCatalogValueError) as duplicate:
            self.catalog.create_category(
                seiko["brand_id"],
                " Наручные часы ",
            )
        self.assertEqual(
            duplicate.exception.existing["id"],
            casio["category_id"],
        )

    def test_legacy_per_brand_category_copy_keeps_its_canonical_id(self):
        casio = self.create_product()
        seiko = self.create_product(
            name="Seiko Legacy Category",
            brand="Seiko",
            category="Наручные часы",
        )
        with self.database.transaction() as connection:
            now = "2026-08-03T10:00:00+00:00"
            cursor = connection.execute(
                "INSERT INTO erp_categories "
                "(brand_id, name, normalized_name, active, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (
                    seiko["brand_id"],
                    " наручные часы ",
                    "наручные часы",
                    now,
                    now,
                ),
            )
            legacy_category_id = cursor.lastrowid
            connection.execute(
                "UPDATE catalog_excel_products SET category_id = ? WHERE id = ?",
                (legacy_category_id, seiko["id"]),
            )

        options = self.catalog.list_category_options(
            brand_id=seiko["brand_id"],
            only_used_by_brand=True,
        )
        wristwatch_options = [
            item for item in options
            if item["name"].strip().casefold() == "наручные часы"
        ]
        self.assertEqual(
            [item["id"] for item in wristwatch_options],
            [legacy_category_id],
        )
        products = self.catalog.list_products(
            brand_id=seiko["brand_id"],
            category_id=legacy_category_id,
        )
        self.assertEqual([item["id"] for item in products], [str(seiko["id"])])
        self.assertEqual(products[0]["category_id"], legacy_category_id)
        self.assertEqual(
            self.catalog.products_by_ids([seiko["id"]])[str(seiko["id"])]["category_id"],
            legacy_category_id,
        )

    def test_rename_is_visible_through_shared_product_without_relinking_history(self):
        product = self.create_product(stock=1)
        self.sales.create_sale(
            {
                "id": "rename-sale",
                "source": "Tictactoy",
                "created_at": "2026-07-30",
                "order_number": "REN-1",
                "product_name": product["display_name"],
                "brand": product["display_brand"],
                "category": product["display_category"],
            },
            product["id"],
            1,
            1000,
        )

        self.catalog.rename_brand(product["brand_id"], "Casio Japan")

        current = self.catalog.get_product(
            product["id"],
            include_archived=True,
        )
        self.assertEqual(current["brand"], "Casio Japan")
        self.assertEqual(
            self.sales.get_sale("rename-sale")["product_id"],
            str(product["id"]),
        )

    def test_atomic_full_inventory_scenario_and_idempotency(self):
        product = self.create_product()
        receipt = {
            "id": "receipt-1",
            "number": "ПР-1",
            "receipt_date": "2026-07-30",
        }
        positions = [{
            "product_id": product["id"],
            "quantity": 10,
            "purchase_price": 500,
        }]
        self.receipts.create_receipt(
            receipt,
            positions,
            idempotency_key="receipt-create-1",
        )
        self.receipts.create_receipt(
            receipt,
            positions,
            idempotency_key="receipt-create-1",
        )
        self.assertEqual(self.stock(product["id"]), 10)

        sale_payload = {
            "id": "sale-1",
            "source": "Tictactoy",
            "created_at": "2026-07-30",
            "order_number": "ORDER-1",
            "product_name": product["display_name"],
            "brand": product["display_brand"],
            "category": product["display_category"],
        }
        self.sales.create_sale(
            sale_payload,
            product["id"],
            3,
            1000,
            idempotency_key="sale-create-1",
            enforce_external_unique=True,
        )
        self.assertEqual(self.stock(product["id"]), 7)

        self.sales.update_sale(
            "sale-1",
            {**sale_payload, "note": "Информационное обновление"},
            3,
            1000,
            idempotency_key="sale-update-1",
        )
        self.assertEqual(self.stock(product["id"]), 7)
        self.assertEqual(
            self.sales.get_sale("sale-1")["note"],
            "Информационное обновление",
        )

        cancelled = self.sales.cancel_sale(
            "sale-1",
            idempotency_key="sale-cancel-1",
        )
        self.sales.cancel_sale(
            "sale-1",
            idempotency_key="sale-cancel-1",
        )
        self.assertEqual(cancelled["status"], "returned")
        self.assertEqual(self.stock(product["id"]), 10)

        self.receipts.update_receipt(
            "receipt-1",
            {**receipt, "number": "ПР-1"},
            [{**positions[0], "quantity": 6}],
            idempotency_key="receipt-update-1",
        )
        self.assertEqual(self.stock(product["id"]), 6)

        movements = self.sales.list_movements(product["id"])
        self.assertEqual(len(movements), 4)
        self.assertEqual(
            {item["type"] for item in movements},
            {"receipt", "sale", "manual_adjustment", "cancellation"},
        )
        self.assertTrue(
            any(item["sale_id"] == "sale-1" for item in movements)
        )
        self.assertTrue(
            any(item["receipt_id"] == "receipt-1" for item in movements)
        )

    def test_receipt_document_is_optional_and_retry_is_stock_neutral(self):
        product = self.create_product(name="Optional Document Product")
        receipt = {
            "id": "receipt-without-document",
            "number": "",
            "receipt_date": "2026-08-21",
        }
        positions = [{
            "product_id": product["id"],
            "quantity": 2,
            "purchase_price": 125,
        }]

        first = self.receipts.create_receipt(
            receipt,
            positions,
            idempotency_key="receipt-without-document-once",
        )
        repeated = self.receipts.create_receipt(
            {**receipt, "id": "duplicate-click-id"},
            positions,
            idempotency_key="receipt-without-document-once",
        )

        self.assertIsNone(first["number"])
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(self.stock(product["id"]), 2)
        with self.database.connect() as connection:
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM erp_receipts"
            ).fetchone()[0]
            movement_count = connection.execute(
                "SELECT COUNT(*) FROM catalog_stock_movements "
                "WHERE receipt_id = ?",
                (first["id"],),
            ).fetchone()[0]
        self.assertEqual(receipt_count, 1)
        self.assertEqual(movement_count, 1)

    def test_comment_only_receipt_update_preserves_stock_and_movements(self):
        product = self.create_product()
        receipt = {
            "id": "receipt-comment-only",
            "number": "ПР-COMMENT",
            "receipt_date": "2026-08-04",
            "comment": "Исходный комментарий",
        }
        positions = [{
            "product_id": product["id"],
            "quantity": 4,
            "purchase_price": 500,
        }]
        self.receipts.create_receipt(receipt, positions)
        stock_before = self.stock(product["id"])
        movements_before = self.sales.list_movements(product["id"])

        self.receipts.update_receipt(
            receipt["id"],
            {**receipt, "comment": "Сохранённый QA-комментарий"},
            positions,
            idempotency_key="receipt-comment-only-update",
        )

        reopened = self.receipts.get_receipt(receipt["id"])
        self.assertEqual(reopened["comment"], "Сохранённый QA-комментарий")
        self.assertEqual(self.stock(product["id"]), stock_before)
        self.assertEqual(
            self.sales.list_movements(product["id"]),
            movements_before,
        )

    def test_draft_keeps_new_shared_product_at_zero_until_posting(self):
        product = self.create_product(
            name="Draft New Product",
            brand="Draft Brand",
            category="Draft Category",
        )
        draft = self.receipts.create_draft(
            {
                "id": "draft-receipt",
                "number": "DRAFT-1",
                "receipt_date": "2026-07-31",
            },
            [{
                "product_id": product["id"],
                "quantity": 4,
                "purchase_price": 10,
            }],
            user_name="company-a-user",
            tenant_id="company-a",
        )

        self.assertEqual(draft["status"], "draft")
        self.assertEqual(self.stock(product["id"]), 0)
        sale_options = self.catalog.list_products(
            brand_id=product["brand_id"],
            category_id=product["category_id"],
        )
        self.assertEqual(
            [item["id"] for item in sale_options],
            [str(product["id"])],
        )

        updated = self.receipts.update_draft(
            draft["id"],
            {
                "id": draft["id"],
                "number": "DRAFT-1",
                "receipt_date": "2026-07-31",
            },
            [{
                "product_id": product["id"],
                "quantity": 5,
                "purchase_price": 10,
            }],
            user_name="company-a-user",
        )
        self.assertEqual(updated["items"][0]["quantity"], 5)
        self.assertEqual(self.stock(product["id"]), 0)

    def test_posting_sets_status_last_and_writes_one_movement_per_line(self):
        first = self.create_product(name="Receipt Line One")
        second = self.create_product(name="Receipt Line Two")
        draft = self.receipts.create_draft(
            {
                "id": "multi-line-receipt",
                "number": "PR-MULTI",
                "receipt_date": "2026-07-31",
            },
            [
                {
                    "product_id": first["id"],
                    "quantity": 2,
                    "purchase_price": 1,
                },
                {
                    "product_id": second["id"],
                    "quantity": 3,
                    "purchase_price": 1,
                },
            ],
        )
        observed_status = []

        def observe_status(connection):
            observed_status.append(
                connection.execute(
                    "SELECT status FROM erp_receipts WHERE id = ?",
                    (draft["id"],),
                ).fetchone()["status"]
            )

        posted = self.receipts.post_receipt(
            draft["id"],
            user_name="poster",
            failure_hook=observe_status,
        )
        repeated = self.receipts.post_receipt(draft["id"], user_name="poster")

        self.assertEqual(observed_status, ["draft"])
        self.assertEqual(posted["status"], "posted")
        self.assertEqual(repeated["status"], "posted")
        self.assertEqual(self.stock(first["id"]), 2)
        self.assertEqual(self.stock(second["id"]), 3)
        with self.database.connect() as connection:
            movements = connection.execute(
                "SELECT * FROM catalog_stock_movements "
                "WHERE receipt_id = ? ORDER BY receipt_item_id",
                (draft["id"],),
            ).fetchall()
        self.assertEqual(len(movements), 2)
        self.assertEqual(
            [row["source_line_id"] for row in movements],
            [str(item["id"]) for item in posted["items"]],
        )
        self.assertTrue(all(row["operation_kind"] == "post" for row in movements))
        self.assertTrue(all(row["stock_before"] == 0 for row in movements))
        self.assertTrue(all(row["source_number"] == "PR-MULTI" for row in movements))

    def test_receipt_post_does_not_duplicate_existing_product(self):
        product = self.create_product(name="Existing Shared Product")
        with self.database.connect() as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM catalog_excel_products"
            ).fetchone()[0]
        self.receipts.create_receipt(
            {
                "id": "existing-product-receipt",
                "number": "PR-EXISTING",
                "receipt_date": "2026-07-31",
            },
            [{
                "product_id": product["id"],
                "quantity": 1,
                "purchase_price": 1,
            }],
        )
        with self.database.connect() as connection:
            after = connection.execute(
                "SELECT COUNT(*) FROM catalog_excel_products"
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(self.stock(product["id"]), 1)

    def test_receipt_operation_uniqueness_is_tenant_scoped(self):
        product = self.create_product(name="Tenant Product")
        statement = (
            "INSERT INTO catalog_stock_movements "
            "(id, product_id, movement_type, quantity_delta, stock_before, "
            "stock_after, tenant_id, source_type, source_id, source_line_id, "
            "operation_kind, created_at) "
            "VALUES (?, ?, 'manual_adjustment', 1, 0, 1, ?, "
            "'receipt', 'same-source', 'same-line', 'post', ?)"
        )
        with self.database.transaction() as connection:
            connection.execute(
                statement,
                (
                    str(uuid.uuid4()),
                    product["id"],
                    "company-a",
                    "2026-07-31T10:00:00+00:00",
                ),
            )
            connection.execute(
                statement,
                (
                    str(uuid.uuid4()),
                    product["id"],
                    "company-b",
                    "2026-07-31T10:00:01+00:00",
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    statement,
                    (
                        str(uuid.uuid4()),
                        product["id"],
                        "company-a",
                        "2026-07-31T10:00:02+00:00",
                    ),
                )

    def test_pr_2026_0002_bitrix_card_stays_visible_after_receipt(self):
        product = self.create_product(
            name="a.b.art Vintage Edge Brown",
            brand="A.B. Art",
            category="Очки",
        )
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog_excel_batches ("
                "id, file_sha256, source_filename, row_count, total_stock, "
                "positive_rows, zero_rows, status, created_at, applied_at"
                ") VALUES ('bitrix-synthetic', 'bitrix-synthetic-sha', "
                "'bitrix-sync', 0, 0, 0, 0, 'superseded', ?, ?)",
                ("2026-07-31T08:00:00+00:00", "2026-07-31T08:00:00+00:00"),
            )
            connection.execute(
                "UPDATE catalog_excel_products SET "
                "source_key = 'bitrix:243444', "
                "current_batch_id = 'bitrix-synthetic', "
                "stock_source = 'bitrix_catalog' WHERE id = ?",
                (product["id"],),
            )

        self.receipts.create_receipt(
            {
                "id": "production-pr-2026-0002",
                "number": "PR-2026-0002",
                "receipt_date": "2026-07-31",
            },
            [{
                "product_id": product["id"],
                "quantity": 29,
                "purchase_price": 0,
            }],
            idempotency_key="production-pr-2026-0002",
        )

        result = self.products.list_products(
            query=product["excel_name_raw"],
            hide_zero=True,
            sort_by="stock",
            sort_dir="desc",
        )
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], product["id"])
        self.assertEqual(result["items"][0]["stock"], 29)
        self.assertEqual(self.products.get_product(product["id"])["stock"], 29)

        ExcelProductBatchService(self.database).apply(
            [{
                "excel_row": 2,
                "excel_name": "Unrelated Excel Product",
                "excel_brand": "Other",
                "excel_article": "OTHER-1",
                "article_quality": "code_like",
                "category": "Other",
                "stock": 1,
                "stock_valid": True,
                "cell": "A-1",
                "product_id": None,
                "match_status": "not_found",
                "match_method": "test",
                "confidence": 0,
                "alternatives": [],
            }],
            "f" * 64,
            "later.xlsx",
        )
        preserved = self.catalog.get_product(
            product["id"],
            include_archived=True,
        )
        self.assertTrue(preserved["active"])
        self.assertEqual(preserved["stock"], 29)

    def test_receipt_without_price_posts_stock_and_stores_null(self):
        product = self.create_product(name="Unknown Cost Product")
        receipt = self.receipts.create_receipt(
            {
                "id": "receipt-without-price",
                "number": "PR-NO-PRICE",
                "receipt_date": "2026-08-06",
            },
            [{"product_id": product["id"], "quantity": 3}],
        )
        self.assertEqual(self.stock(product["id"]), 3)
        self.assertIsNone(receipt["items"][0]["purchase_price"])
        with self.database.connect() as connection:
            sale_price = next(
                row for row in connection.execute("PRAGMA table_info(erp_sale_items)")
                if row["name"] == "unit_price"
            )
            receipt_price = next(
                row for row in connection.execute("PRAGMA table_info(erp_receipt_items)")
                if row["name"] == "purchase_price"
            )
        self.assertEqual(sale_price["notnull"], 0)
        self.assertEqual(receipt_price["notnull"], 0)

    def test_posted_edit_uses_delta_and_cancel_keeps_reverse_history(self):
        product = self.create_product(name="Editable Receipt Product")
        receipt = {
            "id": "editable-receipt",
            "number": "PR-EDIT",
            "receipt_date": "2026-07-31",
        }
        self.receipts.create_receipt(
            receipt,
            [{
                "product_id": product["id"],
                "quantity": 5,
                "purchase_price": 1,
            }],
        )
        self.receipts.update_receipt(
            receipt["id"],
            receipt,
            [{
                "product_id": product["id"],
                "quantity": 7,
                "purchase_price": 1,
            }],
            idempotency_key="edit-delta-once",
        )
        self.assertEqual(self.stock(product["id"]), 7)

        cancelled = self.receipts.cancel_receipt(
            receipt["id"],
            idempotency_key="cancel-once",
        )
        repeated = self.receipts.cancel_receipt(
            receipt["id"],
            idempotency_key="cancel-once",
        )
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(repeated["status"], "cancelled")
        self.assertEqual(self.stock(product["id"]), 0)
        with self.database.connect() as connection:
            movements = connection.execute(
                "SELECT movement_type, quantity_delta, operation_kind "
                "FROM catalog_stock_movements WHERE receipt_id = ? "
                "ORDER BY created_at, rowid",
                (receipt["id"],),
            ).fetchall()
        self.assertEqual(
            [row["movement_type"] for row in movements],
            ["receipt", "manual_adjustment", "cancellation"],
        )
        self.assertEqual(
            [row["quantity_delta"] for row in movements],
            [5, 2, -7],
        )
        self.assertEqual(movements[-1]["operation_kind"], "cancel")

    def test_return_and_failures_change_stock_only_once_and_rollback(self):
        product = self.create_product(stock=3)
        sale_payload = {
            "id": "return-sale",
            "source": "Amazon",
            "created_at": "2026-07-30",
            "order_number": "AMZ-1",
        }
        self.sales.create_sale(
            sale_payload,
            product["id"],
            2,
            1000,
        )
        self.sales.return_sale(
            "return-sale",
            1,
            idempotency_key="return-once",
        )
        self.sales.return_sale(
            "return-sale",
            1,
            idempotency_key="return-once",
        )
        self.assertEqual(self.stock(product["id"]), 2)

        def fail(_connection):
            raise RuntimeError("forced rollback")

        with self.assertRaises(RuntimeError):
            self.receipts.create_receipt(
                {
                    "id": "failed-receipt",
                    "number": "FAIL",
                    "receipt_date": "2026-07-30",
                },
                [{
                    "product_id": product["id"],
                    "quantity": 5,
                    "purchase_price": 1,
                }],
                idempotency_key="failed-receipt",
                failure_hook=fail,
            )
        self.assertEqual(self.stock(product["id"]), 2)
        self.assertFalse(self.receipts.exists("failed-receipt"))

    def test_archive_keeps_sale_receipt_and_movement_history(self):
        product = self.create_product()
        self.receipts.create_receipt(
            {
                "id": "archive-receipt",
                "number": "АРХ-1",
                "receipt_date": "2026-07-30",
            },
            [{
                "product_id": product["id"],
                "quantity": 1,
                "purchase_price": 1,
            }],
        )
        self.sales.create_sale(
            {
                "id": "archive-sale",
                "source": "Wildberries",
                "created_at": "2026-07-30",
            },
            product["id"],
            1,
            100,
        )
        self.products.archive_product(product["id"])

        self.assertIsNone(self.products.get_product(product["id"]))
        archived = self.catalog.get_product(
            product["id"],
            include_archived=True,
        )
        self.assertFalse(archived["active"])
        self.assertIsNotNone(self.sales.get_sale("archive-sale"))
        self.assertIsNotNone(self.receipts.get_receipt("archive-receipt"))
        self.assertEqual(len(self.sales.list_movements(product["id"])), 2)

    def test_external_order_uniqueness_is_scoped_by_source(self):
        product = self.create_product(stock=2)
        first = self.sales.create_sale(
            {
                "id": "source-one",
                "source": "Tictactoy",
                "order_number": "42",
            },
            product["id"],
            1,
            100,
            enforce_external_unique=True,
        )
        repeated = self.sales.create_sale(
            {
                "id": "source-duplicate",
                "source": "Tictactoy",
                "order_number": "42",
            },
            product["id"],
            1,
            100,
            enforce_external_unique=True,
        )
        other_source = self.sales.create_sale(
            {
                "id": "other-source",
                "source": "Amazon",
                "order_number": "42",
            },
            product["id"],
            1,
            100,
            enforce_external_unique=True,
        )

        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(other_source["id"], "other-source")
        self.assertEqual(self.stock(product["id"]), 0)

    def test_migration_is_backed_up_repeatable_and_stock_neutral(self):
        product = self.create_product(stock=7)
        database_path = Path(self.database.path)
        backup = backup_database(
            database_path,
            Path(self.temp.name) / "backups",
        )
        before, after, _audit = migrate(database_path)

        self.assertTrue(backup.exists())
        self.assertEqual(before["products"], after["products"])
        self.assertEqual(before["stock_total"], 7)
        self.assertEqual(after["stock_total"], 7)
        self.assertTrue(migration_applied(database_path))
        self.assertEqual(self.stock(product["id"]), 7)

    def test_old_movement_constraint_is_migrated_without_losing_rows(self):
        product = self.create_product(stock=1)
        with self.database.connect() as connection:
            original_count = connection.execute(
                "SELECT COUNT(*) FROM catalog_stock_movements"
            ).fetchone()[0]
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'catalog_stock_movements'"
            ).fetchone()[0]
            old_sql = table_sql.replace(
                "CREATE TABLE catalog_stock_movements",
                "CREATE TABLE catalog_stock_movements_old",
            ).replace("'cancellation',", "")
            connection.commit()
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(old_sql)
            connection.execute(
                "INSERT INTO catalog_stock_movements_old "
                "SELECT * FROM catalog_stock_movements"
            )
            connection.execute("DROP TABLE catalog_stock_movements")
            connection.execute(
                "ALTER TABLE catalog_stock_movements_old "
                "RENAME TO catalog_stock_movements"
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")

        with sqlite3.connect(str(self.database.path)) as connection:
            connection.row_factory = sqlite3.Row
            apply_fresh_catalog_schema(connection)
            apply_remove_collections_migration(connection)
            apply_incoming_receipts_migration(connection)
        self.database.initialize()

        with self.database.connect() as connection:
            migrated_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'catalog_stock_movements'"
            ).fetchone()[0]
            migrated_count = connection.execute(
                "SELECT COUNT(*) FROM catalog_stock_movements"
            ).fetchone()[0]
        self.assertIn("'cancellation'", migrated_sql)
        self.assertEqual(migrated_count, original_count)
        self.assertEqual(self.stock(product["id"]), 1)

    def test_legacy_receipt_schema_is_migrated_without_losing_history(self):
        product = self.create_product(name="Legacy Schema Product")
        receipt = self.receipts.create_receipt(
            {
                "id": "legacy-schema-receipt",
                "number": "PR-LEGACY-SCHEMA",
                "receipt_date": "2026-07-30",
            },
            [{
                "product_id": product["id"],
                "quantity": 2,
                "purchase_price": 1,
            }],
            idempotency_key="legacy-schema-once",
        )
        with self.database.connect() as connection:
            connection.commit()
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "CREATE TABLE erp_receipts_old ("
                "id TEXT PRIMARY KEY, number TEXT, "
                "status TEXT NOT NULL DEFAULT 'posted' CHECK ("
                "status IN ('posted', 'cancelled')), "
                "receipt_date TEXT NOT NULL, user_name TEXT, "
                "idempotency_key TEXT UNIQUE, "
                "metadata_json TEXT NOT NULL DEFAULT '{}', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "cancelled_at TEXT)"
            )
            connection.execute(
                "INSERT INTO erp_receipts_old "
                "(id, number, status, receipt_date, user_name, idempotency_key, "
                "metadata_json, created_at, updated_at, cancelled_at) "
                "SELECT id, number, status, receipt_date, user_name, "
                "idempotency_key, metadata_json, created_at, updated_at, "
                "cancelled_at FROM erp_receipts"
            )
            connection.execute("DROP TABLE erp_receipts")
            connection.execute(
                "ALTER TABLE erp_receipts_old RENAME TO erp_receipts"
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")

        with sqlite3.connect(str(self.database.path)) as connection:
            connection.row_factory = sqlite3.Row
            apply_fresh_catalog_schema(connection)
            apply_remove_collections_migration(connection)
            apply_incoming_receipts_migration(connection)
        self.database.initialize()

        restored = self.receipts.get_receipt(receipt["id"])
        self.assertEqual(restored["status"], "posted")
        self.assertEqual(restored["tenant_id"], "default")
        self.assertEqual(restored["items"][0]["product_id"], product["id"])
        self.assertEqual(self.stock(product["id"]), 2)
        with self.database.connect() as connection:
            receipt_sql = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'erp_receipts'"
            ).fetchone()[0]
            violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        self.assertIn("'draft'", receipt_sql)
        self.assertEqual(violations, [])

    def test_exact_legacy_links_are_persisted_without_merging_cards(self):
        product = self.create_product()
        instance_dir = Path(self.temp.name)
        (instance_dir / "receipts.json").write_text(
            json.dumps([{
                "id": "legacy-receipt",
                "positions": [{
                    "product_id": "moysklad-a168",
                    "product_name": "Casio A168",
                    "brand": "casio",
                    "category": "Наручные часы",
                }],
            }]),
            encoding="utf-8",
        )
        (instance_dir / "manual_sales.json").write_text(
            json.dumps([{
                "id": "legacy-sale",
                "product_id": "",
                "product_name": "Casio A168",
                "brand": "Casio",
                "category": "Наручные часы",
            }]),
            encoding="utf-8",
        )

        audit = audit_legacy_links(self.database.path, instance_dir)
        persisted = persist_legacy_audit(self.database.path, audit)

        self.assertEqual(persisted["linked"], 2)
        self.assertEqual(persisted["ambiguous"], 0)
        self.assertEqual(
            self.catalog.legacy_links(
                "receipt",
                ["legacy-receipt"],
            )[("legacy-receipt", 0)],
            str(product["id"]),
        )
        self.assertEqual(
            self.catalog.get_product(product["id"])["moysklad_product_id"],
            "moysklad-a168",
        )

    def test_unmatched_legacy_receipt_is_materialized_with_shared_ids(self):
        instance_dir = Path(self.temp.name)
        (instance_dir / "receipts.json").write_text(
            json.dumps([{
                "id": "orphan-receipt",
                "positions": [{
                    "product_id": "moysklad-orphan",
                    "product_name": "Legacy Test",
                    "article": "LEGACY-1",
                    "brand": "Legacy Brand",
                    "category": "Legacy Category",
                    "cell": "L-1",
                    "quantity": 1,
                }],
            }]),
            encoding="utf-8",
        )

        before, after, audit = migrate(
            Path(self.database.path),
            instance_dir,
        )
        reconciliation = audit["legacy_reconciliation"]
        created = reconciliation["materialized"]["created"]

        self.assertEqual(len(created), 1)
        self.assertEqual(after["products"], before["products"] + 1)
        self.assertEqual(after["stock_total"], before["stock_total"])
        self.assertEqual(reconciliation["persisted"]["linked"], 1)
        self.assertEqual(reconciliation["persisted"]["unmatched"], 0)

        product = self.catalog.get_product(created[0]["product_id"])
        self.assertEqual(product["name"], "Legacy Test")
        self.assertEqual(product["brand"], "Legacy Brand")
        self.assertEqual(product["category"], "Legacy Category")
        self.assertEqual(product["moysklad_product_id"], "moysklad-orphan")
        self.assertIsInstance(product["brand_id"], int)
        self.assertIsInstance(product["category_id"], int)
        self.assertEqual(
            self.catalog.legacy_links(
                "receipt",
                ["orphan-receipt"],
            )[("orphan-receipt", 0)],
            product["id"],
        )

    def test_delete_draft_removes_document_without_changing_stock(self):
        product = self.create_product(name="Draft Delete", stock=4)
        self.receipts.create_draft(
            {"id": "delete-draft", "number": "D-1", "receipt_date": "2026-09-10"},
            [{"product_id": product["id"], "quantity": 3, "purchase_price": 1}],
        )
        preview = self.receipts.preview_delete("delete-draft")
        self.assertEqual(preview["items"][0]["status"], "DRAFT_DELETE")
        self.receipts.delete_receipt("delete-draft", user_name="Максим")
        self.assertEqual(self.stock(product["id"]), 4)
        self.assertFalse(self.receipts.exists("delete-draft"))

    def test_delete_preview_is_completely_read_only(self):
        product = self.create_product(name="Preview Read Only")
        self.receipts.create_receipt(
            {"id": "preview-read-only", "number": "P-RO", "receipt_date": "2026-09-10"},
            [{"product_id": product["id"], "quantity": 5, "purchase_price": 1}],
        )
        with self.database.connect() as connection:
            before = {
                "receipts": connection.execute("SELECT COUNT(*) FROM erp_receipts").fetchone()[0],
                "items": connection.execute("SELECT COUNT(*) FROM erp_receipt_items").fetchone()[0],
                "movements": connection.execute("SELECT COUNT(*) FROM catalog_stock_movements").fetchone()[0],
                "sales": connection.execute("SELECT COUNT(*) FROM erp_sales").fetchone()[0],
                "audit": connection.execute("SELECT COUNT(*) FROM erp_audit_events").fetchone()[0],
            }
        stock_before = self.stock(product["id"])
        self.receipts.preview_delete("preview-read-only")
        with self.database.connect() as connection:
            after = {
                "receipts": connection.execute("SELECT COUNT(*) FROM erp_receipts").fetchone()[0],
                "items": connection.execute("SELECT COUNT(*) FROM erp_receipt_items").fetchone()[0],
                "movements": connection.execute("SELECT COUNT(*) FROM catalog_stock_movements").fetchone()[0],
                "sales": connection.execute("SELECT COUNT(*) FROM erp_sales").fetchone()[0],
                "audit": connection.execute("SELECT COUNT(*) FROM erp_audit_events").fetchone()[0],
            }
        self.assertEqual(after, before)
        self.assertEqual(self.stock(product["id"]), stock_before)

    def test_delete_posted_receipt_decreases_multiple_stocks_and_audits(self):
        first = self.create_product(name="Delete A", stock=2)
        second = self.create_product(name="Delete B", stock=5)
        self.receipts.create_receipt(
            {"id": "delete-posted", "number": "P-1", "receipt_date": "2026-09-10"},
            [
                {"product_id": first["id"], "quantity": 3, "purchase_price": 1},
                {"product_id": second["id"], "quantity": 2, "purchase_price": 1},
            ],
        )
        preview = self.receipts.preview_delete("delete-posted")
        self.assertEqual(preview["decreased_positions"], 2)
        self.assertEqual(preview["decreased_quantity"], 5)
        self.assertEqual(self.stock(first["id"]), 5)
        result = self.receipts.delete_receipt("delete-posted", user_name="Максим")
        self.assertEqual(result["decreased_positions"], 2)
        self.assertEqual(self.stock(first["id"]), 2)
        self.assertEqual(self.stock(second["id"]), 5)
        with self.database.connect() as connection:
            audit = connection.execute(
                "SELECT * FROM erp_audit_events WHERE entity_id = 'delete-posted' "
                "AND action = 'deleted'"
            ).fetchone()
        self.assertEqual(audit["actor_display_name_snapshot"], "Максим")
        details = json.loads(audit["metadata_json"])
        self.assertEqual(details["items"][0]["current_stock"], 5)
        self.assertEqual(details["items"][0]["stock_after"], 2)

    def test_later_active_sale_skips_whole_position_and_sale_is_unchanged(self):
        product = self.create_product(name="Later Sale")
        self.receipts.create_receipt(
            {"id": "sale-skip", "number": "P-SALE", "receipt_date": "2026-09-10"},
            [{"product_id": product["id"], "quantity": 5, "purchase_price": 1}],
        )
        self.sales.create_sale(
            {"id": "later-sale", "source": "ERP", "created_at": "2099-09-14T10:00:00+00:00", "order_number": "21147"},
            product["id"], 3, 10,
        )
        before = self.sales.get_sale("later-sale")
        preview = self.receipts.preview_delete("sale-skip")
        self.assertEqual(preview["items"][0]["status"], "SKIPPED_DUE_TO_LATER_SALE")
        self.assertEqual(preview["items"][0]["sales"][0]["number"], "21147")
        self.assertIn("последующая продажа товара", preview["items"][0]["reason"])
        self.receipts.delete_receipt("sale-skip")
        self.assertEqual(self.stock(product["id"]), 2)
        self.assertEqual(self.sales.get_sale("later-sale"), before)
        with self.database.connect() as connection:
            audit = connection.execute(
                "SELECT metadata_json FROM erp_audit_events "
                "WHERE entity_id = 'sale-skip' AND action = 'deleted'"
            ).fetchone()
        skipped = json.loads(audit["metadata_json"])["items"][0]
        self.assertEqual(skipped["sale_ids"], ["later-sale"])
        self.assertEqual(skipped["sale_numbers"], ["21147"])
        self.assertTrue(skipped["sale_dates"][0])
        self.assertEqual(
            skipped["reason"],
            "После проведения поставки зафиксирована последующая продажа товара; "
            "остаток при удалении поставки не изменён.",
        )

    def test_earlier_and_fully_returned_sales_do_not_skip_receipt_rollback(self):
        product = self.create_product(name="Inactive Sales", stock=2)
        self.sales.create_sale(
            {"id": "earlier-sale", "source": "ERP", "created_at": "2020-01-01T10:00:00+00:00"},
            product["id"], 1, 10,
        )
        self.receipts.create_receipt(
            {"id": "active-sale-filter", "number": "P-FILTER", "receipt_date": "2026-09-10"},
            [{"product_id": product["id"], "quantity": 3, "purchase_price": 1}],
        )
        self.sales.create_sale(
            {"id": "returned-sale", "source": "ERP", "created_at": "2099-01-01T10:00:00+00:00"},
            product["id"], 1, 10,
        )
        self.sales.cancel_sale("returned-sale")
        preview = self.receipts.preview_delete("active-sale-filter")
        self.assertEqual(preview["items"][0]["status"], "WILL_DECREASE")
        self.receipts.delete_receipt("active-sale-filter")
        self.assertEqual(self.stock(product["id"]), 1)

    def test_all_positions_with_later_sales_are_skipped_but_receipt_is_deleted(self):
        first = self.create_product(name="All Skipped A")
        second = self.create_product(name="All Skipped B")
        self.receipts.create_receipt(
            {"id": "all-skipped", "number": "P-ALL", "receipt_date": "2026-09-10"},
            [
                {"product_id": first["id"], "quantity": 2, "purchase_price": 1},
                {"product_id": second["id"], "quantity": 3, "purchase_price": 1},
            ],
        )
        self.sales.create_sale(
            {"id": "all-sale-a", "source": "ERP", "created_at": "2026-09-11"},
            first["id"], 1, 10,
        )
        self.sales.create_sale(
            {"id": "all-sale-b", "source": "ERP", "created_at": "2026-09-11"},
            second["id"], 1, 10,
        )
        stock_before = (self.stock(first["id"]), self.stock(second["id"]))
        preview = self.receipts.preview_delete("all-skipped")
        self.assertEqual(preview["decreased_positions"], 0)
        self.assertEqual(preview["decreased_quantity"], 0)
        self.assertEqual(preview["skipped_positions"], 2)
        self.assertFalse(preview["has_conflicts"])
        self.receipts.delete_receipt("all-skipped")
        self.assertEqual((self.stock(first["id"]), self.stock(second["id"])), stock_before)
        self.assertFalse(self.receipts.exists("all-skipped"))

    def test_mixed_delete_and_new_sale_between_preview_and_delete(self):
        first = self.create_product(name="No Sale")
        second = self.create_product(name="Late Change")
        self.receipts.create_receipt(
            {"id": "mixed-delete", "number": "P-MIX", "receipt_date": "2026-09-10"},
            [
                {"product_id": first["id"], "quantity": 4, "purchase_price": 1},
                {"product_id": second["id"], "quantity": 3, "purchase_price": 1},
            ],
        )
        preview = self.receipts.preview_delete("mixed-delete")
        self.assertEqual(preview["skipped_positions"], 0)
        self.sales.create_sale(
            {"id": "sale-after-preview", "source": "ERP", "created_at": "2099-09-15T10:00:00+00:00"},
            second["id"], 1, 10,
        )
        result = self.receipts.delete_receipt("mixed-delete")
        self.assertEqual(result["skipped_positions"], 1)
        self.assertEqual(self.stock(first["id"]), 0)
        self.assertEqual(self.stock(second["id"]), 2)

    def test_delete_recalculates_changed_stock_after_preview(self):
        product = self.create_product(name="Changed Stock", stock=3)
        self.receipts.create_receipt(
            {"id": "changed-stock", "number": "P-STOCK", "receipt_date": "2026-09-10"},
            [{"product_id": product["id"], "quantity": 5, "purchase_price": 1}],
        )
        preview = self.receipts.preview_delete("changed-stock")
        self.assertEqual(preview["items"][0]["stock_after"], 3)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET stock = 10 WHERE id = ?", (product["id"],)
            )
        result = self.receipts.delete_receipt("changed-stock")
        self.assertEqual(result["items"][0]["current_stock"], 10)
        self.assertEqual(result["items"][0]["stock_after"], 5)
        self.assertEqual(self.stock(product["id"]), 5)

    def test_delete_conflict_and_failure_roll_back_everything(self):
        product = self.create_product(name="Conflict Product")
        self.receipts.create_receipt(
            {"id": "conflict-delete", "number": "P-C", "receipt_date": "2026-09-10"},
            [{"product_id": product["id"], "quantity": 5, "purchase_price": 1}],
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET stock = 2 WHERE id = ?", (product["id"],)
            )
        preview = self.receipts.preview_delete("conflict-delete")
        self.assertTrue(preview["has_conflicts"])
        with self.assertRaises(ValueError):
            self.receipts.delete_receipt("conflict-delete")
        self.assertTrue(self.receipts.exists("conflict-delete"))
        self.assertEqual(self.stock(product["id"]), 2)

        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET stock = 5 WHERE id = ?", (product["id"],)
            )
        with self.assertRaises(RuntimeError):
            self.receipts.delete_receipt(
                "conflict-delete", failure_hook=lambda _connection: (_ for _ in ()).throw(RuntimeError("fail"))
            )
        self.assertTrue(self.receipts.exists("conflict-delete"))
        self.assertEqual(self.stock(product["id"]), 5)

    def test_multi_position_delete_failure_rolls_back_stock_items_and_sales(self):
        products = [self.create_product(name="Rollback {}".format(index)) for index in range(3)]
        self.receipts.create_receipt(
            {"id": "multi-rollback", "number": "P-RB", "receipt_date": "2026-09-10"},
            [{"product_id": product["id"], "quantity": index + 1, "purchase_price": 1} for index, product in enumerate(products)],
        )
        self.sales.create_sale(
            {"id": "preserved-sale", "source": "ERP", "created_at": "2020-01-01"},
            products[0]["id"], 1, 10,
        )
        stocks = [self.stock(product["id"]) for product in products]
        sale = self.sales.get_sale("preserved-sale")
        with self.database.connect() as connection:
            item_count = connection.execute(
                "SELECT COUNT(*) FROM erp_receipt_items WHERE receipt_id = 'multi-rollback'"
            ).fetchone()[0]
        with self.assertRaises(RuntimeError):
            self.receipts.delete_receipt(
                "multi-rollback",
                failure_hook=lambda _connection: (_ for _ in ()).throw(RuntimeError("forced")),
            )
        self.assertEqual([self.stock(product["id"]) for product in products], stocks)
        self.assertEqual(self.sales.get_sale("preserved-sale"), sale)
        self.assertTrue(self.receipts.exists("multi-rollback"))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM erp_receipt_items WHERE receipt_id = 'multi-rollback'"
            ).fetchone()[0], item_count)

    def test_edit_receipt_details_is_stock_neutral_and_audited(self):
        product = self.create_product(name="Edit Details")
        self.receipts.create_receipt(
            {"id": "details-edit", "number": "P-E", "receipt_date": "2026-09-10"},
            [{"product_id": product["id"], "quantity": 2, "purchase_price": 1}],
        )
        updated = self.receipts.update_details(
            "details-edit", "Сентябрьская поставка", "Новый комментарий", user_name="Максим"
        )
        self.assertEqual(updated["metadata"]["name"], "Сентябрьская поставка")
        self.assertEqual(updated["comment"], "Новый комментарий")
        self.assertEqual(updated["number"], "P-E")
        self.assertEqual(updated["status"], "posted")
        self.assertEqual(updated["items"][0]["quantity"], 2)
        self.assertEqual(self.stock(product["id"]), 2)
        with self.database.connect() as connection:
            audit = connection.execute(
                "SELECT changes_json FROM erp_audit_events "
                "WHERE entity_id = 'details-edit' AND action = 'updated' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(
            json.loads(audit["changes_json"])["comment"]["after"],
            "Новый комментарий",
        )


if __name__ == "__main__":
    unittest.main()
