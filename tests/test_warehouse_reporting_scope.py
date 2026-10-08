"""Company reporting must not silently fall back to the legacy TTT column.

Disposable SQLite data, Flask test client and synthetic documents only.
"""
import html
import inspect
import json
import re
import unittest
from unittest.mock import patch

from flask import template_rendered

import test_warehouse_catalog_totals as fixture
from app.services.business_analytics import BusinessAnalytics, parse_filters
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.manual_receipts import ManualReceipts
from app.services.shared_catalog import SharedCatalog
from app.services.warehouse_stock_scope import WarehouseStockScope
from app.services.warehouse_transfers import WarehouseTransfers


class WarehouseReportingScopeTest(unittest.TestCase):
    product = fixture.WarehouseCatalogTotalsTest.product

    def setUp(self):
        fixture.WarehouseCatalogTotalsTest.setUp(self)
        with self.db.transaction() as connection:
            connection.execute(
                'INSERT OR IGNORE INTO erp_brand_categories(brand_id,category_id,created_at) VALUES(?,?,?)',
                (self.brand_id, self.watch_category, '2026-10-08'))

    def get(self, path, **query):
        from app import web
        contexts = []

        def rendered(sender, template, context, **extra):
            contexts.append(context)

        with patch.dict(web.app.config, TESTING=True, AUTH_TESTING=False), patch.dict(
            'os.environ', {'CATALOG_DATABASE_PATH': str(self.db.path)}
        ), template_rendered.connected_to(rendered, web.app):
            response = web.app.test_client().get(path, query_string=query)
        return response, contexts[-1] if contexts else None

    def assert_reports(self, warehouse, units, available=1, models=1):
        excel = ExcelProductCatalog(self.db)
        for method in ('list_brand_overviews', 'list_brand_summaries'):
            row = next(row for row in getattr(self.catalog, method)(warehouse_id=warehouse)
                       if row['id'] == self.brand_id)
            self.assertEqual((row['stock_total'], row['nonzero_count'], row['product_count']),
                             (units, available, models), method)
        brand = self.catalog.get_brand_overview(self.brand_id, warehouse_id=warehouse)
        self.assertEqual(brand['stock_total'], units)
        self.assertEqual(sum(row['stock_total'] for row in brand['categories']), units)
        category = self.catalog.get_category_overview(self.watch_category, warehouse_id=warehouse)
        self.assertEqual((category['stock_total'], category['nonzero_count'], category['product_count']),
                         (units, available, models))
        self.assertEqual(sum(row['stock_total'] for row in category['brands']), units)
        # Name and aggregate sorting have separate SQL paths.
        for sort in ('name', 'stock', 'in_stock'):
            rows = self.catalog.list_category_overviews(
                query='Наручные', warehouse_id=warehouse, sort_by=sort, include_brands=False)['items']
            self.assertEqual(rows[0]['stock_total'], units)
        counts = excel.stock_tab_counts(warehouse_id=warehouse)
        self.assertEqual((counts['units_total'], counts['in_stock'], counts['positions']),
                         (units, available, models))
        structure = excel.stock_analytics(self.watch_category, warehouse_id=warehouse)
        self.assertEqual(structure['summary']['total_stock'], units)
        self.assertEqual(structure['summary']['models'], models)
        self.assertEqual(sum(row['stock'] for row in structure['models']), units)
        self.assertEqual(sum(row['units'] for row in excel.product_analytics(
            warehouse_id=warehouse)['top_brands']), units)

    def test_hong_kong_277_is_in_every_company_report_not_in_ttt(self):
        self.assert_reports('all', 277)
        self.assert_reports('hong-kong', 277)
        self.assert_reports('default', 0, available=0)
        self.assertEqual(self.catalog.get_brand_overview(self.brand_id)['stock_total'], 277)
        self.assertEqual(ExcelProductCatalog(self.db).stock_tab_counts()['units_total'], 277)

    def test_one_sku_on_two_warehouses_counted_once(self):
        receipt = ManualReceipts(self.db).create('default', 'initial_stock', [
            {'product_id': self.pid, 'quantity': 23}])
        ManualReceipts(self.db).post(receipt['id'])
        self.assert_reports('all', 300)
        self.assert_reports('default', 23)
        self.assert_reports('hong-kong', 277)

    def test_transit_is_counted_once_but_never_available(self):
        transfers = WarehouseTransfers(self.db)
        document = transfers.create('hong-kong', 'default', [
            {'product_id': self.pid, 'quantity': 277}], 'report-test')
        transfers.transition(document['id'], 'send')
        self.assert_reports('all', 277, available=0)
        self.assert_reports('hong-kong', 0, available=0)
        self.assert_reports('default', 0, available=0)
        self.assertEqual(ExcelProductCatalog(self.db).stock_tab_counts()['units_in_stock'], 0)
        for view, key in [('brands', 'brands'), ('categories', 'categories')]:
            response, context = self.get('/app/products', view=view)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(any(row['stock_total'] == 277 for row in context[key]))
        transfers.transition(document['id'], 'receive')
        self.assert_reports('all', 277)
        self.assert_reports('default', 277)

    def test_dynamic_third_warehouse_and_invalid_scope(self):
        warehouse = "third'{}-warehouse"
        with self.db.transaction() as connection:
            connection.execute(
                'INSERT INTO erp_warehouses(id,code,name,active,created_at,updated_at) VALUES(?,?,?,1,?,?)',
                (warehouse, 'third', 'Третий', '2026-10-08', '2026-10-08'))
        receipts = ManualReceipts(self.db)
        document = receipts.create(warehouse, 'initial_stock', [
            {'product_id': self.pid, 'quantity': 13}])
        receipts.post(document['id'])
        self.assert_reports(warehouse, 13)
        self.assert_reports('all', 290)
        for value in ('missing', '', None, "x' OR 1=1 --"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.catalog.list_brand_summaries(warehouse_id=value)
        with self.db.transaction() as connection:
            connection.execute('UPDATE erp_warehouses SET active=0 WHERE id=?', (warehouse,))
        with self.assertRaises(ValueError):
            self.catalog.list_category_overviews(warehouse_id=warehouse)
        # Deactivating a warehouse must not erase its company-owned stock.
        self.assert_reports('all', 290)

    def test_api_scope_search_sort_and_no_empty_hong_kong_brand(self):
        for warehouse, units in [('all', 277), ('hong-kong', 277), ('default', 0)]:
            response, _ = self.get('/api/v1/brands', warehouse_id=warehouse, q='Луч')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['data']['items'][0]['stock_total'], units)
            response, _ = self.get('/api/v1/category-overviews', warehouse_id=warehouse,
                                   q='Наручные', sort_by='stock', limit=1)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['data'][0]['stock_total'], units)
        for path in ('/api/v1/brands', '/api/v1/category-overviews'):
            response, _ = self.get(path, show_empty='0')
            self.assertEqual(response.status_code, 200)
            data = response.get_json()['data']
            self.assertTrue(data['items'] if isinstance(data, dict) else data)
            response, _ = self.get(path, warehouse_id='missing')
            self.assertEqual(response.status_code, 422)

    def test_report_pages_default_to_all_and_explicit_scope_survives_links(self):
        for view in ('brands', 'categories', 'analytics'):
            response, context = self.get('/app/products', view=view)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(context['selected_warehouse'], 'all')
            self.assertEqual(context['product_metrics']['units'], '277')
            self.assertIn('id="warehouseSelector"', response.get_data(as_text=True))
            for warehouse, units in [('default', '0'), ('hong-kong', '277')]:
                response, context = self.get('/app/products', view=view, warehouse_id=warehouse)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(context['product_metrics']['units'], units)
                markup = response.get_data(as_text=True)
                if view == 'brands':
                    # JS URLs require JSON escaping, not HTML &amp; query keys.
                    dynamic_url = re.search(r'link.href=("(?:[^"\\]|\\.)*")\+', markup)
                    self.assertIsNotNone(dynamic_url)
                    self.assertNotIn('&amp;', json.loads(dynamic_url.group(1)))
                tab_links = re.findall(r'href="([^"]+)" data-products-tab=', markup)
                self.assertEqual(len(tab_links), 4)
                self.assertTrue(all('warehouse_id=' + warehouse in html.unescape(link)
                                    for link in tab_links))
        response, context = self.get('/app/products')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(context['selected_warehouse'], 'default')
        self.assertEqual(context['product_metrics']['units'], '0')

    def test_detail_and_delete_preview_have_separate_stock_scopes(self):
        response, context = self.get('/app/products', view='brands', brand_id=self.brand_id,
                                     warehouse_id='default')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(context['brand']['stock_total'], 0)
        self.assertEqual(context['brand_delete']['stock_total'], 277)
        self.assertEqual(context['brand_delete']['categories'][0]['stock_total'], 277)
        delete_payload = re.search(r'data-delete-category="([^"]+)"', response.get_data(as_text=True))
        self.assertEqual(json.loads(html.unescape(delete_payload.group(1)))['stock_total'], 277)
        response, context = self.get('/app/products', view='categories', category_id=self.watch_category,
                                     warehouse_id='hong-kong')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(context['category']['stock_total'], 277)

    def test_business_stock_report_uses_company_on_hand_not_ttt_or_transit(self):
        service = BusinessAnalytics(self.db)
        def rows(state):
            with self.db.connect() as connection:
                return service._stock_rows(connection, parse_filters({'stock_state': state}))
        result = rows('positive')
        self.assertEqual(result['rows'][0]['stock'], 277)
        self.assertEqual(rows('out')['rows'], [])
        transfers = WarehouseTransfers(self.db)
        document = transfers.create('hong-kong', 'default', [
            {'product_id': self.pid, 'quantity': 277}], 'report-transit')
        transfers.transition(document['id'], 'send')
        self.assertEqual(rows('positive')['rows'], [])
        self.assertEqual(rows('out')['rows'][0]['stock'], 0)

    def test_sale_from_hong_kong_and_business_product_stock_stay_consistent(self):
        from app.services.sales_inventory import SalesInventory
        SalesInventory(self.db).create_sale(
            {'source': 'amazon', 'warehouse_id': 'hong-kong', 'created_at': '2026-10-08T12:00:00+03:00'},
            self.pid, 2, 100)
        with self.db.connect() as connection:
            result = BusinessAnalytics(self.db)._product_rows(connection, parse_filters({
                'from': '2026-10-08', 'to': '2026-10-08'}), revenue_complete=True)
        self.assertEqual(result['rows'][0]['stock'], 275)
        self.assertEqual(result['rows'][0]['units'], 2)
        self.assertEqual(result['rows'][0]['revenue'], 200)
        self.assert_reports('all', 275)
        self.assert_reports('default', 0, available=0)

    def test_reports_are_read_only_and_use_shared_projection(self):
        def snapshot():
            with self.db.connect() as connection:
                return {table: [tuple(row) for row in connection.execute('SELECT * FROM ' + table)]
                        for table in ('erp_warehouse_stocks', 'catalog_stock_movements', 'catalog_excel_products')}
        before = snapshot()
        for warehouse in ('all', 'default', 'hong-kong'):
            self.assert_reports(warehouse, 0 if warehouse == 'default' else 277,
                                available=0 if warehouse == 'default' else 1)
        self.assertEqual(before, snapshot())
        for method in (SharedCatalog.list_brand_overviews, SharedCatalog.list_brand_summaries,
                       SharedCatalog.list_category_overviews, ExcelProductCatalog.stock_tab_counts,
                       ExcelProductCatalog.product_analytics, ExcelProductCatalog.stock_analytics):
            source = inspect.getsource(method)
            self.assertIn('WarehouseStockScope', source)
            # Global category rename impact counts all cards, not scoped stock.
            self.assertNotIn('FROM catalog_excel_products',
                             source.replace('FROM catalog_excel_products all_p ', ''))
        with self.db.connect() as connection, self.assertRaises(ValueError):
            WarehouseStockScope(connection).execute(connection, 'UPDATE reporting_products SET stock=0')


if __name__ == '__main__':
    unittest.main()
