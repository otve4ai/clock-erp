import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import web
from app.catalog_db import CatalogDatabase
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.product_category_backfill import ProductCategoryBackfill
from app.services.shared_catalog import SharedCatalog


class ProductCategoryBackfillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = CatalogDatabase(
            Path(self.temp.name) / "catalog.db", cache_initialization=False
        )
        self.database.initialize()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog_excel_batches (id, file_sha256, "
                "source_filename, row_count, total_stock, positive_rows, "
                "zero_rows, status, created_at, applied_at) VALUES "
                "('category-backfill', 'sha', 'test.xlsx', 0, 0, 0, 0, "
                "'active', '2026-09-15T00:00:00+00:00', "
                "'2026-09-15T00:00:00+00:00')"
            )
        self.products = ExcelProductCatalog(self.database)
        self.catalog = SharedCatalog(self.database)
        self.backfill = ProductCategoryBackfill(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, name, brand="Casio", category="Ремни", stock=0):
        return self.products.create_product(
            name=name,
            article=name.upper().replace(" ", "-"),
            brand=brand,
            category=category,
            stock=stock,
        )

    def make_legacy(self, product_id, clear_brand=True, bitrix_category=None):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET category_id = NULL, "
                "brand_id = CASE WHEN ? THEN NULL ELSE brand_id END, "
                "bitrix_category = ? WHERE id = ?",
                (1 if clear_brand else 0, bitrix_category, product_id),
            )

    def test_canonical_product_stays_in_category_and_out_of_system_bucket(self):
        product = self.create("Canonical", stock=2)

        category = self.catalog.list_category_overviews(limit=100)["items"]
        by_name = {item["name"]: item for item in category}

        self.assertEqual(by_name["Ремни"]["product_count"], 1)
        self.assertEqual(by_name["Без категории"]["product_count"], 0)
        self.assertEqual(
            self.products.list_products(category_id=product["category_id"])["total"],
            1,
        )

    def test_preview_and_apply_assign_unique_category_and_brand(self):
        product = self.create("Legacy strap", stock=7)
        category_id = product["category_id"]
        brand_id = product["brand_id"]
        self.make_legacy(product["id"], bitrix_category="Ремни")

        preview = self.backfill.preview()
        item = preview["items"][0]

        self.assertEqual(item["excel_category"], "Ремни")
        self.assertEqual(item["bitrix_category"], "Ремни")
        self.assertEqual(item["proposed_brand_id"], brand_id)
        self.assertEqual(item["proposed_category_id"], category_id)
        self.assertEqual(item["confidence"], "high")
        self.assertFalse(item["ambiguous"])
        self.assertEqual(
            self.products.list_products(category_id=0)["total"], 1
        )

        result = self.backfill.apply(
            preview["plan_digest"], backup_dir=Path(self.temp.name) / "backups"
        )
        updated = self.products.get_product(product["id"])

        self.assertEqual(result["writes_performed"], 1)
        self.assertEqual(updated["category_id"], category_id)
        self.assertEqual(updated["brand_id"], brand_id)
        self.assertEqual(updated["stock"], 7)
        self.assertEqual(self.products.list_products(category_id=0)["total"], 0)
        self.assertEqual(
            self.products.list_products(
                brand_id=brand_id, category_id=category_id
            )["total"],
            1,
        )
        with self.database.connect() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM erp_brand_categories "
                "WHERE brand_id = ? AND category_id = ?",
                (brand_id, category_id),
            ).fetchone())

    def test_empty_category_sources_remain_uncategorized(self):
        product = self.create("Actually empty", category="")

        preview = self.backfill.preview()

        self.assertEqual(preview["summary"]["eligible_legacy_products"], 0)
        self.assertEqual(preview["summary"]["current_uncategorized"], 1)
        self.assertEqual(
            preview["summary"]["remaining_uncategorized_after_apply"], 1
        )
        self.assertIsNone(self.products.get_product(product["id"])["category_id"])

    def test_duplicate_category_name_without_safe_brand_is_ambiguous(self):
        first_brand = self.catalog.create_brand("First")
        second_brand = self.catalog.create_brand("Second")
        now = "2026-09-15T00:00:00+00:00"
        with self.database.transaction() as connection:
            for category_id, brand_id in ((701, first_brand["id"]),
                                          (702, second_brand["id"])):
                connection.execute(
                    "INSERT INTO erp_categories (id, brand_id, name, "
                    "normalized_name, active, created_at, updated_at) "
                    "VALUES (?, ?, 'Ремни', 'ремни', 1, ?, ?)",
                    (category_id, brand_id, now, now),
                )
        product = self.create("Ambiguous", brand="", category="")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET excel_category = 'Ремни', "
                "bitrix_category = 'Ремни' WHERE id = ?",
                (product["id"],),
            )

        preview = self.backfill.preview()

        self.assertEqual(preview["summary"]["automatic_matches"], 0)
        self.assertEqual(preview["summary"]["ambiguous"], 1)
        self.assertEqual(preview["items"][0]["reason"], "category_name_ambiguous")
        self.assertIsNone(preview["items"][0]["proposed_category_id"])
        result = self.backfill.apply(
            preview["plan_digest"], backup_dir=Path(self.temp.name) / "backups"
        )
        self.assertEqual(result["writes_performed"], 0)
        self.assertIsNone(self.products.get_product(product["id"])["category_id"])

    def test_duplicate_category_name_is_resolved_only_by_exact_brand_scope(self):
        first_brand = self.catalog.create_brand("First")
        second_brand = self.catalog.create_brand("Second")
        now = "2026-09-15T00:00:00+00:00"
        with self.database.transaction() as connection:
            for category_id, brand_id in ((711, first_brand["id"]),
                                          (712, second_brand["id"])):
                connection.execute(
                    "INSERT INTO erp_categories (id, brand_id, name, "
                    "normalized_name, active, created_at, updated_at) "
                    "VALUES (?, ?, 'Украшения', 'украшения', 1, ?, ?)",
                    (category_id, brand_id, now, now),
                )
        product = self.create("Scoped", brand="", category="")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET excel_brand = 'Second', "
                "bitrix_brand = 'Second', excel_category = 'Украшения', "
                "bitrix_category = 'Украшения' WHERE id = ?",
                (product["id"],),
            )

        preview = self.backfill.preview()
        item = preview["items"][0]

        self.assertFalse(item["ambiguous"])
        self.assertEqual(item["proposed_brand_id"], second_brand["id"])
        self.assertEqual(item["proposed_category_id"], 712)
        self.assertIn("brand_scoped_duplicate_category", item["reason"])

    def test_conflicting_category_sources_are_never_assigned(self):
        product = self.create("Conflict")
        self.catalog.create_brand_category(product["brand_id"], "Украшения")
        self.make_legacy(product["id"], bitrix_category="Украшения")

        preview = self.backfill.preview()
        item = preview["items"][0]

        self.assertTrue(item["ambiguous"])
        self.assertEqual(item["reason"], "category_sources_conflict")
        self.assertIsNone(item["proposed_category_id"])

    def test_main_projection_prefers_canonical_and_marks_legacy_separately(self):
        canonical = web.build_excel_warehouse_items([{
            "id": 1,
            "excel_name_raw": "Canonical",
            "category_id": 9,
            "category_name": "Наручные часы",
            "excel_category": "Старое значение",
            "stock": 0,
        }])[0]
        legacy = web.build_excel_warehouse_items([{
            "id": 2,
            "excel_name_raw": "Legacy",
            "category_id": None,
            "category_name": None,
            "excel_category": "Ремни",
            "stock": 0,
        }])[0]

        self.assertEqual(canonical["category"], "Наручные часы")
        self.assertFalse(canonical["category_is_legacy"])
        self.assertEqual(legacy["category"], "")
        self.assertEqual(legacy["legacy_category"], "Ремни")
        self.assertTrue(legacy["category_is_legacy"])


class UncategorizedRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "catalog.db"
        self.environment = mock.patch.dict(
            "os.environ", {"CATALOG_DATABASE_PATH": str(self.database_path)}
        )
        self.environment.start()
        database = CatalogDatabase(self.database_path, cache_initialization=False)
        database.initialize()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog_excel_batches (id, file_sha256, "
                "source_filename, row_count, total_stock, positive_rows, "
                "zero_rows, status, created_at, applied_at) VALUES "
                "('route', 'sha', 'route.xlsx', 0, 0, 0, 0, 'active', "
                "'2026-09-15T00:00:00+00:00', '2026-09-15T00:00:00+00:00')"
            )
        products = ExcelProductCatalog(database)
        self.canonical = products.create_product(
            name="CANONICAL_ONLY", article="CAN", brand="Casio",
            category="Наручные часы", stock=0,
        )
        self.empty = products.create_product(
            name="UNCATEGORIZED_ONLY", article="NONE", brand="",
            category="", stock=0,
        )
        self.legacy = products.create_product(
            name="LEGACY_ONLY", article="LEG", brand="Casio",
            category="Наручные часы", stock=0,
        )
        with database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET category_id = NULL "
                "WHERE id = ?", (self.legacy["id"],)
            )
        self.original_config = dict(web.app.config)
        web.app.config.update(TESTING=True, AUTH_TESTING=False)
        self.client = web.app.test_client()

    def tearDown(self):
        web.app.config.clear()
        web.app.config.update(self.original_config)
        self.environment.stop()
        self.temp.cleanup()

    def test_category_zero_is_preserved_and_only_uncategorized_rows_are_opened(self):
        response = self.client.get("/app/products?category_id=0&per_page=100")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("UNCATEGORIZED_ONLY", body)
        self.assertIn("LEGACY_ONLY", body)
        self.assertNotIn("CANONICAL_ONLY", body)
        self.assertIn("legacy · не назначена", body)
        self.assertIn("Без категории", body)


if __name__ == "__main__":
    unittest.main()
