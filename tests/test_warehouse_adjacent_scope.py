"""Regression checks for warehouse context outside the main product table."""
import re
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_warehouse_reporting_scope as fixture
from app import web
from app.services.excel_product_catalog import ExcelProductCatalog


class WarehouseAdjacentScopeTest(unittest.TestCase):
    product = fixture.WarehouseReportingScopeTest.product
    get = fixture.WarehouseReportingScopeTest.get

    def setUp(self):
        fixture.WarehouseReportingScopeTest.setUp(self)

    def test_inventory_start_keeps_hk_and_initial_brand_quantity(self):
        response, context = self.get('/app/inventory/run', warehouse_id='hong-kong')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(context['selected_warehouse'], 'hong-kong')
        self.assertEqual(context['brands'][0]['stock_total'], 277)
        body = response.get_data(as_text=True)
        self.assertIn('value="hong-kong" selected', body)
        self.assertIn('data-warehouse-id="hong-kong"', body)
        for path in ('/app/products/inventory', '/app/inventory'):
            response, _ = self.get(path, view='start', warehouse_id='hong-kong')
            self.assertEqual(response.status_code, 302)
            self.assertIn('warehouse_id=hong-kong', response.location)

    def test_company_inventory_requires_one_explicit_warehouse(self):
        response, context = self.get('/app/inventory/run', warehouse_id='all')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(context['selected_warehouse'], '')
        self.assertRegex(response.get_data(as_text=True), r'<fieldset id="inventoryScopeFields"[^>]* disabled>')
        self.assertIn('<option value="" disabled selected>Выберите склад', response.get_data(as_text=True))
        response, _ = self.get('/app/inventory/run', warehouse_id='unknown')
        self.assertEqual(response.status_code, 400)

    def test_global_category_rename_impact_is_not_limited_to_hong_kong(self):
        self.product('Только TTT', ttt=2)
        brand = self.catalog.get_brand_overview(self.brand_id, warehouse_id='hong-kong')
        self.assertEqual(brand['categories'][0]['product_count'], 1)
        self.assertEqual(brand['categories'][0]['global_product_count'], 2)

    def test_products_api_reports_requested_warehouse_not_ttt(self):
        for warehouse, quantity in [('default', 0), ('hong-kong', 277), ('all', 277)]:
            response, _ = self.get('/api/v1/products', warehouse_id=warehouse)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertEqual(float(data['data'][0]['stock']), quantity)
            self.assertEqual(data['meta']['stats']['total_stock'], quantity)
        response, _ = self.get('/api/v1/products', warehouse_id='unknown')
        self.assertEqual(response.status_code, 422)

    def test_site_issue_and_platform_checks_cannot_hide_other_warehouse_stock(self):
        for warehouse in ('hong-kong', 'all'):
            response, context = self.get('/app/products', warehouse_id=warehouse,
                                        site_issue='in_stock_inactive', check_state='complete')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(context['site_issue'], '')
            self.assertEqual(context['check_state'], 'all')
            self.assertEqual(context['product_metrics']['units'], '277')
            response, _ = self.get('/api/v1/products', warehouse_id=warehouse,
                                   site_issue='in_stock_inactive', check_state='complete')
            self.assertEqual(response.get_json()['meta']['stats']['total_stock'], 277)

    def test_non_ttt_zero_list_neither_reads_nor_synchronizes_ttt_check_cycles(self):
        with patch('app.services.excel_product_catalog.OutOfStockChecks') as checks, patch.object(
            web, 'OutOfStockChecks'
        ) as page_checks:
            for warehouse in ('hong-kong', 'all'):
                response, _ = self.get('/app/products', warehouse_id=warehouse, stock_state='out')
                self.assertEqual(response.status_code, 200)
                body = response.get_data(as_text=True)
                self.assertNotIn('<th data-column-key="platform_checks">', body)
                self.assertNotIn('name="check_state"', body)
            checks.assert_not_called()
            page_checks.assert_not_called()
        response, _ = self.get('/app/products', warehouse_id='default', stock_state='out')
        self.assertIn('<th data-column-key="platform_checks">', response.get_data(as_text=True))

    def test_taxonomy_forms_and_redirects_keep_selected_warehouse(self):
        for view in ('brands', 'categories'):
            response, _ = self.get('/app/products', view=view, warehouse_id='hong-kong')
            self.assertEqual(response.status_code, 200)
            forms = re.findall(r'<form\b[^>]*method="post".*?</form>', response.get_data(as_text=True), re.S)
            self.assertTrue(forms)
            for form in forms:
                self.assertIn('name="warehouse_id" value="hong-kong"', form)
        for redirect in (web._brands_redirect, web._categories_redirect):
            for warehouse in ('default', 'hong-kong', 'all'):
                with web.app.test_request_context('/warehouse/brands', method='POST',
                                                 data={'warehouse_id': warehouse}):
                    response = redirect()
                self.assertEqual(parse_qs(urlsplit(response.location).query)['warehouse_id'], [warehouse])

    def test_card_save_refreshes_current_scoped_results_and_invalidates_old_snapshots(self):
        source = (Path(web.PROJECT_ROOT) / 'app/templates/warehouse.html').read_text(encoding='utf-8')
        handler = source.split('document.getElementById("inlineProductForm").addEventListener("submit"', 1)[1]
        handler = handler.split('function filterBrandOptions', 1)[0]
        self.assertIn('warehouseSearchSnapshots.clear();', handler)
        self.assertIn('warehouseSearchBaseline = null;', handler)
        self.assertIn('await loadWarehouseResultsUrl(new URL(window.location.href))', handler)
        self.assertLess(handler.index('warehouseSearchSnapshots.clear()'), handler.index('await loadWarehouseResultsUrl'))
        ExcelProductCatalog(self.db).update_product(
            self.pid, stock=280, stock_warehouse_id='hong-kong', stock_expected=277,
            stock_reason='Synthetic correction')
        with patch.dict(web.app.config, TESTING=True, AUTH_TESTING=False), patch.dict(
            'os.environ', {'CATALOG_DATABASE_PATH': str(self.db.path)}
        ):
            response = web.app.test_client().get('/app/products?warehouse_id=hong-kong',
                                                 headers={'X-ERP-Partial': 'products-v1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('X-ERP-Partial'), 'products-v1')
        self.assertIn('data-metric-units="280"', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
