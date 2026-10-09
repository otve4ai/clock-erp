import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.catalog_db import CatalogDatabase
from app.services.bitrix_catalog_importer import BitrixCatalogImporter
from app.services.bitrix_erp_product_sync import BitrixERPProductSync
from app.services.excel_product_catalog import ExcelProductCatalog
from scripts.repair_time_studio_identity import repair
from tests.test_bitrix_erp_product_sync import product


class TimeStudioIdentityRepairTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = CatalogDatabase(Path(self.temp.name) / "catalog.db")
        self.backups = Path(self.temp.name) / "backups"
        wrong = product("243767", name="Time Studio Black (20 мм)", sku="TSB-20mm", xml_id="243767")
        self.service = BitrixERPProductSync(self.database)
        self.erp_id = self.service.apply_single(wrong, "create", quantity=7)["erp_product_id"]
        BitrixCatalogImporter(self.database).import_products([wrong], mode="full_sync")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET bitrix_catalog_product_id = "
                "(SELECT id FROM catalog_products WHERE external_product_id = '243767') WHERE id = ?",
                (self.erp_id,),
            )
        self.sources = {
            "243767": product("243767", name="Time Studio Black (18 мм)", sku="TSB-18mm", xml_id="243767"),
            "244358": product("244358", name="Time Studio Black (20 мм)", sku="TSB-20mm", xml_id="244358"),
        }
        self.client = mock.Mock()
        self.client.get_product.side_effect = self.sources.get

    def tearDown(self):
        self.temp.cleanup()

    def card(self):
        connection = self.database.connect()
        try:
            return dict(connection.execute("SELECT * FROM catalog_excel_products WHERE id = ?", (self.erp_id,)).fetchone())
        finally:
            connection.close()

    def run_repair(self, apply=True):
        return repair(self.database, self.client, self.erp_id, apply, self.backups)

    def test_repairs_both_links_and_preserves_stock_idempotently(self):
        before = self.card()
        result = self.run_repair()
        self.assertEqual(result["status"], "repaired")
        self.assertTrue(Path(result["backup"]).is_file())
        after = self.card()
        self.assertEqual(after["bitrix_external_product_id"], "244358")
        self.assertEqual(after["source_key"], "bitrix:244358")
        for key in before.keys() - {"bitrix_external_product_id", "bitrix_xml_id", "bitrix_catalog_product_id", "source_key"}:
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after["stock"], 7)
        self.assertFalse(self.service.preview_single(self.sources["243767"])["duplicate"])
        self.assertEqual(self.service.preview_single(self.sources["244358"])["existing"]["id"], self.erp_id)
        self.assertEqual(self.run_repair()["status"], "already_repaired")
        connection = self.database.connect()
        try:
            cached = dict(connection.execute("SELECT external_product_id, article FROM catalog_products").fetchall())
            self.assertEqual(cached, {"243767": "TSB-18mm", "244358": "TSB-20mm"})
        finally:
            connection.close()
        self.assert_new_18mm_can_be_created()

    def assert_new_18mm_can_be_created(self):
        added = self.service.apply_single(self.sources["243767"], "create", quantity=4)
        self.assertNotEqual(added["erp_product_id"], self.erp_id)
        catalog = ExcelProductCatalog(self.database)
        self.assertEqual(catalog.get_product(added["erp_product_id"])["stock"], 4)
        self.assertEqual(catalog.get_product(self.erp_id)["stock"], 7)

    def test_completes_previous_repair_with_stale_source_key(self):
        self.run_repair()
        with self.database.transaction() as connection:
            connection.execute("UPDATE catalog_excel_products SET source_key = 'bitrix:243767' WHERE id = ?", (self.erp_id,))
        before = self.card()
        self.assertEqual(self.run_repair(False)["status"], "preview")
        self.assertEqual(before, self.card())
        self.assertEqual(self.run_repair()["status"], "repaired")
        after = self.card()
        self.assertEqual({key for key in before if before[key] != after[key]}, {"source_key"})
        self.assertEqual(after["source_key"], "bitrix:244358")
        self.assert_new_18mm_can_be_created()

    def test_preview_does_not_write_or_create_backup(self):
        before = self.card()
        self.assertEqual(self.run_repair(False)["status"], "preview")
        self.assertEqual(before, self.card())
        self.assertFalse(self.backups.exists())

    def test_rejects_changed_source_before_writing(self):
        before = self.card()
        self.sources["244358"]["external_sku"] = "unexpected"
        with self.assertRaisesRegex(ValueError, "Live Bitrix"):
            self.run_repair()
        self.assertEqual(before, self.card())
        self.assertFalse(self.backups.exists())

    def test_audit_failure_rolls_back_cache_and_erp(self):
        before = self.card()
        with mock.patch("scripts.repair_time_studio_identity.AuditJournal.record", side_effect=ValueError("audit failed")):
            with self.assertRaisesRegex(ValueError, "audit failed"):
                self.run_repair()
        self.assertEqual(before, self.card())
        connection = self.database.connect()
        try:
            cached = dict(connection.execute("SELECT external_product_id, article FROM catalog_products").fetchall())
            self.assertEqual(cached, {"243767": "TSB-20mm"})
        finally:
            connection.close()

    def test_rejects_new_erp_card_before_writing(self):
        ExcelProductCatalog(self.database).create_product(name="Time Studio Black (18 мм)", article="TSB-18mm")
        before = self.card()
        with self.assertRaisesRegex(ValueError, "ERP cards"):
            self.run_repair()
        self.assertEqual(before, self.card())
        self.assertFalse(self.backups.exists())

    def test_rejects_source_key_owned_by_inactive_card(self):
        other = ExcelProductCatalog(self.database).create_product(name="Other", article="OTHER")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET source_key = 'bitrix:244358', active = 0 WHERE id = ?",
                (other["id"],),
            )
        before = self.card()
        with self.assertRaisesRegex(ValueError, "already occupied"):
            self.run_repair()
        self.assertEqual(before, self.card())
        self.assertFalse(self.backups.exists())
