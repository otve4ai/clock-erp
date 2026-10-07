"""HTTP regressions for the HK pilot, with existing isolated fixtures only."""
import unittest
import re
import shutil
import subprocess

from app.services.component_inventory import balance
from app.services.manual_receipts import ManualReceipts
from app.services.warehouse_transfers import WarehouseTransfers
import test_order_tictactoy_sale as order_fixture
import test_unified_catalog_api as api_fixture


class HongKongWorkflowsTest(unittest.TestCase):
    def check_inline_scripts(self, html):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node required for rendered inline JavaScript syntax check')
        for attributes, script in re.findall(r'<script\b([^>]*)>(.*?)</script>', html, re.S | re.I):
            if 'src=' in attributes or not script.strip() or 'application/' in attributes:
                continue
            checked = subprocess.run([node,'--check','-'],input=script,encoding='utf-8',
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            self.assertEqual(checked.returncode,0,checked.stderr)

    def fixture(self, factory):
        case = factory()
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    def test_order_selects_hk_and_cancel_returns_to_hk(self):
        case = self.fixture(order_fixture.OrderTictactoySaleTest)
        transfers = WarehouseTransfers(case.database)
        doc = transfers.create('default', 'hong-kong', [
            {'product_id':case.watch['id'], 'quantity':4},
            {'product_id':case.strap['id'], 'quantity':2},
        ], 'hk-order-fixture')
        transfers.transition(doc['id'], 'send')
        transfers.transition(doc['id'], 'receive')
        response = case.render_order('?warehouse_id=hong-kong&open_sale=1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="hong-kong" selected', response.text)
        self.check_inline_scripts(response.text)
        response = case.conduct(warehouse_id='hong-kong')
        self.assertEqual(response.status_code, 302)
        sale = case.inventory.find_active_sale('tictactoy','18593')
        self.assertIsNotNone(sale, response.location)
        self.assertEqual(sale['warehouse_id'], 'hong-kong')
        with case.database.connect() as c:
            self.assertEqual(balance(c, case.watch['id']), 1)
            self.assertEqual(balance(c, case.watch['id'], warehouse_id='hong-kong'), 2)
        case.conduct(warehouse_id='default')  # Repeated order must not spend TTT.
        case.inventory.cancel_sale(sale['id'], reason='fixture', user_name='test')
        with case.database.connect() as c:
            self.assertEqual(balance(c, case.watch['id']), 1)
            self.assertEqual(balance(c, case.watch['id'], warehouse_id='hong-kong'), 4)

    def test_order_does_not_fall_back_to_ttt(self):
        case = self.fixture(order_fixture.OrderTictactoySaleTest)
        for warehouse in ('hong-kong', 'missing', 'all'):
            response = case.conduct(warehouse_id=warehouse)
            self.assertEqual(response.status_code, 302)
            self.assertIsNone(case.inventory.find_active_sale('tictactoy','18593'))
            if warehouse == 'hong-kong':
                self.assertIn('warehouse_id=hong-kong',response.location)
        with case.database.connect() as c:
            self.assertEqual(balance(c, case.watch['id']), 5)

    def test_manual_form_explicit_hk_and_stock_error(self):
        case = self.fixture(api_fixture.UnifiedCatalogApiTest)
        from app.catalog_db import CatalogDatabase
        from app.services.sales_inventory import SalesInventory
        db = CatalogDatabase(case.database_path)
        receipts = ManualReceipts(db)
        doc = receipts.create('hong-kong','initial_stock',[{'product_id':case.product['id'],'quantity':2}])
        receipts.post(doc['id'])
        payload = {'source':'Tictactoy','warehouse_id':'hong-kong','created_at':'2026-10-05',
                   'product_id':str(case.product['id']),'quantity':'1','original_unit_price':'1000',
                   'idempotency_key':'manual-hk-fixture'}
        result = case.client.post('/sales/manual/add',data=payload)
        self.assertEqual(result.status_code, 302)
        with db.connect() as c:
            sale = c.execute('SELECT id,warehouse_id FROM erp_sales WHERE idempotency_key=?',('manual-hk-fixture',)).fetchone()
            self.assertIsNotNone(sale, result.location)
            self.assertEqual(sale['warehouse_id'],'hong-kong')
            self.assertEqual(balance(c,case.product['id']),0)
            self.assertEqual(balance(c,case.product['id'],warehouse_id='hong-kong'),1)
        result = case.client.patch('/api/v1/sales/'+sale['id'],json={'warehouse_id':'default'})
        self.assertGreaterEqual(result.status_code,400)
        self.assertEqual(SalesInventory(db).get_sale(sale['id'])['warehouse_id'],'hong-kong')
        payload.update(warehouse_id='missing',idempotency_key='invalid-hk')
        self.assertEqual(case.client.post('/sales/manual/add',data=payload).status_code,302)
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM erp_sales').fetchone()[0],1)

    def test_rendered_forms_have_warehouse_choice_and_valid_scripts(self):
        case = self.fixture(api_fixture.UnifiedCatalogApiTest)
        from app import web
        with web.app.test_request_context():
            inventory_url = web.url_for('inventory_run_page')
        for path, select_id in [('/sales?source=amazon','saleWarehouse'),
                                ('/sales?source=writeoff','writeoffWarehouse'),
                                ('/app/receipts','supply-warehouse'),
                                (inventory_url,'inventoryWarehouse')]:
            with self.subTest(path=path):
                response = case.client.get(path)
                self.assertEqual(response.status_code,200)
                self.assertIn('id="'+select_id+'"',response.text)
                if select_id == 'saleWarehouse':
                    control = response.text.split('id="saleWarehouseCombobox"', 1)[1]
                    control = control.split('>Товар</h3>', 1)[0]
                    self.assertEqual(set(re.findall(r'data-brand="([^"]*)"', control)),
                                     {'default', 'hong-kong'})
                    hidden = re.search(r'<input\b[^>]*id="saleWarehouse"[^>]*>', control).group()
                    self.assertIn('type="hidden"', hidden)
                    self.assertIn('name="warehouse_id"', hidden)
                    self.assertIn('disabled', hidden)
                    self.assertIn('id="saleWarehouseComboboxTrigger"', control)
                else:
                    self.assertIn('value="hong-kong"',response.text)
                    self.assertNotIn('value="wb"',response.text)
                self.check_inline_scripts(response.text)


if __name__ == '__main__':
    unittest.main()
