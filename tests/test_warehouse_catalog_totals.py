"""Warehouse-scoped cascade metrics on disposable catalog data only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.catalog_db import CatalogDatabase
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.manual_receipts import ManualReceipts
from app.services.shared_catalog import SharedCatalog, format_stock_value
from app.services.supplies import SupplyEngine
from app.services.warehouse_transfers import WarehouseTransfers


class WarehouseCatalogTotalsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = CatalogDatabase(Path(self.temp.name) / 'catalog.db')
        self.db.initialize()
        self.catalog = SharedCatalog(self.db)
        self.sequence = 0
        self.catalog.create_brand('Луч')
        self.watch_category = self.catalog.create_global_category('Наручные часы')['id']
        self.pid = self.product('77471760', hong_kong=277)
        self.brand_id = self.catalog.get_product(self.pid)['brand_id']

    def product(self, name, hong_kong=0, ttt=0, brand='Луч', category=None):
        self.sequence += 1
        pid = int(SupplyEngine(self.db).resolve_bitrix({
            'external_product_id': 'synthetic-{}'.format(self.sequence),
            'external_sku': 'TEST-{}'.format(self.sequence),
            'name': name, 'brand': brand,
        })['id'])
        with self.db.transaction() as connection:
            connection.execute('UPDATE catalog_excel_products SET category_id=? WHERE id=?',
                               (self.watch_category if category is None else category, pid))
        receipts = ManualReceipts(self.db)
        for warehouse, quantity in [('default', ttt), ('hong-kong', hong_kong)]:
            if quantity:
                document = receipts.create(warehouse, 'initial_stock', [
                    {'product_id': pid, 'quantity': quantity},
                ])
                receipts.post(document['id'])
        return pid

    def options(self, kind, **kwargs):
        from app import web
        params = dict(type=kind, warehouse_id='hong-kong', available_for_sale='1')
        params.update(kwargs)
        with patch.dict(web.app.config, TESTING=True, AUTH_TESTING=False), patch.dict(
            'os.environ', {'CATALOG_DATABASE_PATH': str(self.db.path)}
        ):
            response = web.app.test_client().get('/api/v1/catalog/options', query_string=params)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['data']

    def assert_metrics(self, option, quantity, count):
        self.assertEqual(option['stock_total'], quantity)
        self.assertEqual(option['stock_display'], format_stock_value(quantity))
        self.assertEqual(option['product_count'], count)
        self.assertEqual(option['count'], count)  # SKU count is not a quantity.

    def test_277_in_hong_kong_and_zero_in_ttt_across_cascade(self):
        self.assert_metrics(self.options('brand')[0], 277, 1)
        self.assert_metrics(self.options('category', brand_id=self.brand_id)[0], 277, 1)
        self.assert_metrics(self.options('model', brand_id=self.brand_id,
                                         category_id=self.watch_category)[0], 277, 1)
        product = self.options('product', brand_id=self.brand_id,
                               category_id=self.watch_category)[0]
        self.assertEqual(product['stock'], 277)
        self.assertEqual(product['warehouse_id'], 'hong-kong')
        self.assertEqual(self.options('brand', warehouse_id='default'), [])
        self.assert_metrics(self.options('brand', warehouse_id='default', available_for_sale='0')[0], 0, 1)

    def test_totals_respect_brand_category_and_warehouse_not_result_limit(self):
        self.product('Вторые часы', hong_kong=23, ttt=900)
        strap_category = self.catalog.create_global_category('Ремни')['id']
        self.product('Ремень', hong_kong=11, category=strap_category)
        self.product('Другой бренд', hong_kong=31, brand='Другой')
        brand = self.options('brand', q='Луч', limit=1)[0]
        self.assert_metrics(brand, 311, 3)
        categories = {row['id']: row for row in self.options('category', brand_id=self.brand_id)}
        self.assert_metrics(categories[self.watch_category], 300, 2)
        self.assert_metrics(categories[strap_category], 11, 1)
        limited = self.options('category', brand_id=self.brand_id, q='Наручные', limit=1)
        self.assert_metrics(limited[0], 300, 2)
        self.assert_metrics(self.options('brand', warehouse_id='default', q='Луч')[0], 900, 1)

    def test_missing_balance_has_explicit_zero_metrics_when_zero_stock_is_allowed(self):
        self.product('Пустой товар', ttt=17, brand='Пустой')
        self.assertEqual(self.options('brand', q='Пустой'), [])
        empty = self.options('brand', q='Пустой', available_for_sale='0')[0]
        self.assert_metrics(empty, 0, 1)
        category = self.options('category', brand_id=empty['id'], available_for_sale='0')[0]
        self.assert_metrics(category, 0, 1)

    def test_transit_is_excluded_until_received(self):
        ExcelProductCatalog(self.db).update_product(self.pid, stock=10, stock_reason='test')
        transfers = WarehouseTransfers(self.db)
        document = transfers.create('default', 'hong-kong', [
            {'product_id': self.pid, 'quantity': 4},
        ], 'synthetic-transfer')
        transfers.transition(document['id'], 'send')
        self.assert_metrics(self.options('brand')[0], 277, 1)
        transfers.transition(document['id'], 'receive')
        self.assert_metrics(self.options('brand')[0], 281, 1)
        self.assert_metrics(self.options('category', brand_id=self.brand_id)[0], 281, 1)

    def test_inactive_deleted_and_inventory_locked_products_are_excluded(self):
        inactive = self.product('Архивный', hong_kong=20)
        deleted = self.product('Удалённый', hong_kong=30)
        with self.db.transaction() as connection:
            connection.execute('UPDATE catalog_excel_products SET active=0 WHERE id=?', (inactive,))
            connection.execute('UPDATE catalog_excel_products SET deleted_at=? WHERE id=?', ('2026-10-06', deleted))
        self.assert_metrics(self.options('brand')[0], 277, 1)
        self.assert_metrics(self.options('category', brand_id=self.brand_id)[0], 277, 1)
        from app.services.brand_inventory import BrandInventory
        BrandInventory(self.db).start(self.brand_id, warehouse_id='hong-kong')
        self.assertEqual(self.options('brand'), [])
        self.assertEqual(self.options('category', brand_id=self.brand_id), [])

    def test_aggregate_reads_do_not_change_balances_or_movements(self):
        def snapshot():
            with self.db.connect() as connection:
                return {
                    table: [tuple(row) for row in connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                    for table in ('erp_warehouse_stocks', 'catalog_excel_products', 'catalog_stock_movements')
                }
        before = snapshot()
        for _ in range(2):
            self.options('brand')
            self.options('category', brand_id=self.brand_id)
            self.options('product', brand_id=self.brand_id, category_id=self.watch_category)
        self.assertEqual(snapshot(), before)


if __name__ == '__main__':
    unittest.main()
