"""Warehouse-specific visibility on synthetic data, without live integrations."""
import re
import subprocess
import unittest
from pathlib import Path

import test_single_bitrix_product_import as fixtures
from app.catalog_db import CatalogDatabase


ROOT = Path(__file__).resolve().parents[1]


class WarehouseSiteColumnTest(unittest.TestCase):
    setUp = fixtures.SingleBitrixProductImportTest.setUp
    tearDown = fixtures.SingleBitrixProductImportTest.tearDown
    taxonomy = fixtures.SingleBitrixProductImportTest.taxonomy
    post_import = fixtures.SingleBitrixProductImportTest.post_import

    def test_full_and_partial_pages_hide_the_site_column_everywhere_except_ttt(self):
        response = self.post_import(fixtures.source_product('701', image=False),
                                    warehouse_id='hong-kong', quantity=3)
        self.assertEqual(response.status_code, 201)
        for warehouse, hidden in [('default', False), ('hong-kong', True),
                                  ('default', False), ('all', True), ('default', False)]:
            for partial in (False, True):
                with self.subTest(warehouse=warehouse, partial=partial):
                    response = self.client.get('/app/products', query_string={
                        'warehouse_id': warehouse,
                    }, headers={'X-ERP-Partial': 'products-v1'} if partial else {})
                    self.assertEqual(response.status_code, 200)
                    markup = response.text
                    self.assertEqual(response.headers.get('X-ERP-Partial') == 'products-v1', partial)
                    self.assertIn('data-site-status-hidden="{}"'.format(str(hidden).lower()), markup)
                    for tag in ('col', 'th', 'td'):
                        cells = re.findall(r'<{}\b[^>]*data-column-key="site_status"[^>]*>'.format(tag), markup)
                        self.assertTrue(cells, tag)
                        self.assertTrue(all((' hidden' in cell) == hidden for cell in cells))
                    self.assertNotRegex(markup, r'<(?:col|th|td)\b[^>]*data-column-key="stock"[^>]*\shidden')
        with CatalogDatabase(self.database_path).connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM catalog_stock_movements WHERE warehouse_id='hong-kong'"
            ).fetchone()[0], 1)

    def test_column_layout_and_user_preferences_survive_warehouse_switches(self):
        css = (ROOT / 'app/static/css/multiwarehouse.css').read_text(encoding='utf-8')
        self.assertRegex(css, r'\[data-site-status-hidden="true"\] \[data-column-key="site_status"\]\s*\{\s*display:none !important;')
        result = subprocess.run(
            ['node', str(ROOT / 'tests/warehouse_site_column_node.js')],
            capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
