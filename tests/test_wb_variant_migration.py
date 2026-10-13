"""Upgrade and repeat the WB mapping migration on a disposable pre-upgrade DB."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import schema_migrations as migrations


class WbVariantMigrationTest(unittest.TestCase):
    def test_upgrade_preserves_existing_rows_and_repeated_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.db'
            previous = migrations.MIGRATIONS[:-1]
            self.assertEqual(migrations.MIGRATIONS[-1]['id'], migrations.WB_MAPPING_MIGRATION_ID)
            # Construct the prior version without applying the current manifest.
            with mock.patch.object(migrations, 'MIGRATIONS', previous), mock.patch.object(migrations, 'verify_complete_catalog_contract', return_value=True):
                migrations.apply_migrations(path, app_commit='before-wb-mappings')
            with sqlite3.connect(str(path)) as connection:
                before = connection.execute('SELECT COUNT(*) FROM erp_order_product_mappings').fetchone()[0]
                self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='erp_wb_product_mappings'").fetchone())
            migrations.apply_migrations(path, app_commit='with-wb-mappings')
            migrations.apply_migrations(path, app_commit='repeat-wb-mappings')
            with sqlite3.connect(str(path)) as connection:
                self.assertTrue(migrations.verify_complete_catalog_contract(connection))
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM erp_wb_product_mappings').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM erp_order_product_mappings').fetchone()[0], before)
