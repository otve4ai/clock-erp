"""Product drawer metrics use the selected warehouse, not the sale picker."""
import re
import unittest
from pathlib import Path

import test_warehouse_reporting_scope as fixture
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.manual_receipts import ManualReceipts
from app.services.warehouse_transfers import WarehouseTransfers


class WarehouseFilterScopeTest(unittest.TestCase):
    product = fixture.WarehouseReportingScopeTest.product
    get = fixture.WarehouseReportingScopeTest.get

    def setUp(self):
        fixture.WarehouseReportingScopeTest.setUp(self)
        ExcelProductCatalog(self.db).update_product(self.pid, model='77471760')

    def options(self, kind, warehouse, **extra):
        query = dict(type=kind, catalog_scope='warehouse_filter', warehouse_id=warehouse)
        if kind != 'brand':
            query['brand_id'] = self.brand_id
        if kind == 'model':
            query['category_id'] = self.watch_category
        query.update(extra)
        response, _ = self.get('/api/v1/catalog/options', **query)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['data']

    def test_all_three_levels_match_table_for_ttt_hk_and_all(self):
        for warehouse, units in [('default', 0), ('hong-kong', 277), ('all', 277)]:
            for kind in ('brand', 'category', 'model'):
                with self.subTest(warehouse=warehouse, kind=kind):
                    option = self.options(kind, warehouse)[0]
                    self.assertEqual(option['stock_total'], units)
                    self.assertEqual(option['stock_display'], str(units))
                    self.assertEqual(option['count'], 1)
            table = ExcelProductCatalog(self.db).list_products(warehouse_id=warehouse)
            self.assertEqual(sum(item['stock'] for item in table['items']), units)
        self.assertEqual(self.options('model', 'all', q='77471760')[0]['stock_total'], 277)

    def test_two_warehouses_sum_units_without_duplicate_sku(self):
        receipts = ManualReceipts(self.db)
        document = receipts.create('default', 'initial_stock', [{'product_id': self.pid, 'quantity': 28}])
        receipts.post(document['id'])
        for kind in ('brand', 'category', 'model'):
            option = self.options(kind, 'all')[0]
            self.assertEqual((option['stock_total'], option['product_count']), (305, 1))
            self.assertEqual(self.options(kind, 'default')[0]['stock_total'], 28)
        self.assertEqual(self.options('brand', 'all', q='Ничего'), [])

    def test_availability_and_transit_match_product_filters(self):
        for kind in ('brand', 'category', 'model'):
            self.assertEqual(self.options(kind, 'default', stock_state='in'), [])
            self.assertEqual(self.options(kind, 'default', stock_state='out')[0]['stock_total'], 0)
        transfers = WarehouseTransfers(self.db)
        doc = transfers.create('hong-kong', 'default', [{'product_id': self.pid, 'quantity': 277}], 'filter-test')
        transfers.transition(doc['id'], 'send')
        for kind in ('brand', 'category', 'model'):
            self.assertEqual(self.options(kind, 'all')[0]['stock_total'], 277)
            self.assertEqual(self.options(kind, 'all', stock_state='in'), [])
            self.assertEqual(self.options(kind, 'all', stock_state='out')[0]['stock_total'], 277)

    def test_drawer_emits_scope_and_client_includes_it_in_requests_and_cache(self):
        for warehouse in ('default', 'hong-kong', 'all'):
            response, _ = self.get('/app/products', warehouse_id=warehouse)
            self.assertEqual(response.status_code, 200)
            drawer = response.get_data(as_text=True).split('id="filterDrawer"', 1)[1]
            form = re.search(r'<form\s.*?</form>', drawer, re.S).group(0)
            self.assertIn('data-catalog-scope="warehouse_filter"', form)
            self.assertIn('data-warehouse-id="' + warehouse + '"', form)
            self.assertIn('name="warehouse_id" value="' + warehouse + '"', form)
        js = Path('app/static/js/catalog-combobox.js').read_text(encoding='utf-8')
        self.assertIn("parameters.set('warehouse_id', scope.dataset.warehouseId)", js)
        self.assertIn('parameters.set("catalog_scope", scope.dataset.catalogScope)', js)
        self.assertIn("parameters.set('stock_state', new URL(window.location.href)", js)
        self.assertIn('(kind === "product" || kind === "model") ? selectedSharedCatalogId(', js)

    def test_invalid_scope_rejected_and_sales_picker_stays_warehouse_specific(self):
        for warehouse in ('missing', "x' OR 1=1 --"):
            response, _ = self.get('/api/v1/catalog/options', type='brand',
                                   catalog_scope='warehouse_filter', warehouse_id=warehouse)
            self.assertEqual(response.status_code, 422)
        response, _ = self.get('/api/v1/catalog/options', type='brand',
                               available_for_sale='1', warehouse_id='all')
        self.assertEqual(response.status_code, 422)
        response, _ = self.get('/api/v1/catalog/options', type='brand',
                               available_for_sale='1', warehouse_id='hong-kong')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['data'][0]['stock_total'], 277)


if __name__ == '__main__':
    unittest.main()
