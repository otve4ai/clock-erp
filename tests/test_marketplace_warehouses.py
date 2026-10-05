"""Hong Kong pilot with synthetic, isolated SQLite; WB retains the old flow."""
import tempfile
import unittest
from pathlib import Path

from app.catalog_db import CatalogDatabase
from app.services.component_inventory import balance
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.supplies import SupplyEngine
from app.services.sales_inventory import SalesInventory
from app.services.warehouse_transfers import WarehouseTransfers, distribution
from app.services.bitrix_stock_sync import BitrixStockSync
from app.services.bitrix_erp_product_sync import BitrixERPProductSync


class MarketplaceWarehousesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = CatalogDatabase(Path(self.temp.name) / 'catalog.db')
        self.db.initialize()
        self.catalog = ExcelProductCatalog(self.db)
        self.pid = int(SupplyEngine(self.db).resolve_bitrix({
            'external_product_id': 'MP-1', 'name': 'Demo watch', 'external_sku': 'MP-1', 'brand': 'Test'
        })['id'])
        self.catalog.update_product(self.pid, stock=10, stock_reason='opening fixture')
        self.sales = SalesInventory(self.db)
        self.transfers = WarehouseTransfers(self.db)

    def stocks(self):
        with self.db.connect() as c:
            return tuple(balance(c, self.pid, warehouse_id=w) for w in ('default','hong-kong'))

    def owned(self):
        with self.db.connect() as c:
            return sum(r['quantity'] + r['in_transit'] for r in distribution(c, [self.pid])[self.pid])

    def test_amazon_defaults_to_hk_and_cannot_use_ttt_stock(self):
        with self.assertRaises(ValueError):
            self.sales.create_sale({'source':'Amazon'}, self.pid,1,100)
        transfer = self.transfers.create('default','hong-kong',[{'product_id':self.pid,'quantity':4}],'amazon-fixture')
        self.transfers.transition(transfer['id'],'send')
        self.transfers.transition(transfer['id'],'receive')
        sale = self.sales.create_sale({'source':'Amazon'}, self.pid,1,100)
        self.assertEqual(sale['warehouse_id'], 'hong-kong')
        self.assertEqual(self.stocks(), (6,3))
        self.sales.cancel_sale(sale['id'],reason='test',user_name='test')
        self.assertEqual(self.stocks(), (6,4))

    def test_pilot_seeds_hk_only_and_has_no_wb_dispatch_route(self):
        from app import web
        with self.db.connect() as c:
            self.assertEqual({r[0] for r in c.execute('SELECT id FROM erp_warehouses')}, {'default','hong-kong'})
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='erp_wb_fulfillments'").fetchone())
        self.assertNotIn('wildberries_dispatch', web.app.view_functions)

    def test_wb_retains_ttt_sale_without_creating_transfer(self):
        from app.services.wildberries_orders import normalize_wildberries_order
        from app.services.wildberries_sales import WildberriesSales
        order = normalize_wildberries_order({'id':123,'article':'MP-1','convertedFinalPrice':123456,'currencyCode':643})
        service = WildberriesSales(self.sales, lambda order: [
            {'state':'mapped','product':{'id':self.pid,'name':'Demo watch'}} for _ in order['products']])
        sale = service.conduct(order)
        self.assertEqual(sale['warehouse_id'], 'default')
        self.assertEqual(self.stocks(), (9,0))
        self.assertEqual(service.conduct(order)['id'], sale['id'])
        self.assertEqual(self.transfers.list(), [])

    def test_new_warehouse_still_uses_generic_transfer_engine(self):
        with self.db.transaction() as c:
            c.execute("INSERT INTO erp_warehouses VALUES('future','FUTURE','Future fixture',1,0,'2026-10-05','2026-10-05')")
        doc = self.transfers.create('default','future',[{'product_id':self.pid,'quantity':2}],'future-fixture')
        self.transfers.transition(doc['id'],'send')
        self.transfers.transition(doc['id'],'receive')
        with self.db.connect() as c:
            self.assertEqual(balance(c,self.pid,warehouse_id='future'),2)
        self.assertEqual(self.stocks(), (8,0))
        self.assertEqual(self.owned(), 10)

    def test_bitrix_cannot_restore_stock_after_dispatch(self):
        doc = self.transfers.create('default','hong-kong',[{'product_id':self.pid,'quantity':1}],'bitrix-transfer')
        self.transfers.transition(doc['id'], 'send')
        site = {'external_product_id':'MP-1','name':'Demo watch','external_sku':'MP-1',
                'brand':'Test','stock':10,'stock_source_field':'CCatalogProduct.QUANTITY'}
        report = BitrixStockSync(self.db).synchronize([site])
        self.assertEqual(report['items'][0]['erp_stock'], 9)
        self.assertEqual(report['items'][0]['delta'], 1)
        with self.assertRaisesRegex(ValueError, 'ERP — источник'):
            BitrixStockSync(self.db).synchronize([site], apply=True)
        BitrixERPProductSync(self.db).apply_products([site])
        self.assertEqual(self.stocks(), (9,0))
        self.assertEqual(self.owned(), 10)

    def test_new_site_card_imports_initial_stock_to_ttt_only_once(self):
        from app.services.bitrix_catalog_importer import BitrixCatalogImporter
        from test_bitrix_erp_product_sync import product
        site = product('new-site-card', name='New card', brand='Test', stock=100, image=False)
        BitrixCatalogImporter(self.db).import_products([site], 'full_sync')
        result = BitrixERPProductSync(self.db).apply_products([site])[0]
        self.assertEqual(result['status'], 'created', result)
        self.assertEqual(result['stock_imported'], 100)
        with self.db.connect() as c:
            row = c.execute("SELECT id,stock FROM catalog_excel_products WHERE bitrix_external_product_id='new-site-card'").fetchone()
            self.assertEqual(row['stock'], 100)
            self.assertEqual(balance(c, row['id']), 100)
            self.assertEqual(balance(c, row['id'], warehouse_id='hong-kong'), 0)
        doc = self.transfers.create('default','hong-kong',[{'product_id':row['id'],'quantity':30}],'import-transfer')
        self.transfers.transition(doc['id'], 'send')
        self.transfers.transition(doc['id'], 'receive')
        BitrixERPProductSync(self.db).apply_products([site])
        with self.db.connect() as c:
            self.assertEqual(balance(c, row['id']), 70)
            self.assertEqual(balance(c, row['id'], warehouse_id='hong-kong'), 30)

    def test_hk_inventory_records_delta_without_touching_ttt(self):
        from app.services.brand_inventory import BrandInventory
        doc = self.transfers.create('default','hong-kong',[{'product_id':self.pid,'quantity':4}],'count-fixture')
        self.transfers.transition(doc['id'],'send')
        self.transfers.transition(doc['id'],'receive')
        with self.db.connect() as c:
            brand_id = c.execute('SELECT brand_id FROM catalog_excel_products WHERE id=?',(self.pid,)).fetchone()[0]
        inventory = BrandInventory(self.db)
        session, created = inventory.start(brand_id, warehouse_id='hong-kong', idempotency_key='count-hk')
        self.assertTrue(created)
        self.assertEqual(session['warehouse_name'], 'Гонконг')
        item = inventory.list_items(session['id'])[0]
        self.assertEqual(item['snapshot_stock'], 4)
        self.assertEqual(item['current_stock'], 4)
        result = inventory.confirm(session['id'], item['id'], 3, idempotency_key='hk-adjust')
        self.assertEqual(result['delta'], -1)
        self.assertEqual(self.stocks(), (6,3))
        repeated = inventory.confirm(session['id'], item['id'], 3, idempotency_key='hk-adjust')
        self.assertTrue(repeated['repeated'])
        self.assertTrue(inventory.complete(session['id'], confirmation=True)['ok'])
        with self.db.connect() as c:
            movement = c.execute('SELECT warehouse_id,quantity_delta FROM catalog_stock_movements WHERE id=?',(result['movement_id'],)).fetchone()
            self.assertEqual(tuple(movement), ('hong-kong', -1))
        from app.services.inventory_control import InventoryControl
        control = InventoryControl(self.db)
        self.assertIn('Гонконг', control.document(session['id'])['scope_label'])
        self.assertIsNone(control.brand_summary()[0]['latest'])
        self.assertIsNone(inventory.brand_summary()[0]['latest'])
        from app.services.inventory_restoration import InventorySnapshotRestoration, InventoryRestorationError
        with self.assertRaisesRegex(InventoryRestorationError, 'только TTT'):
            InventorySnapshotRestoration(self.db).plan('Test', session['id'])

    def test_hk_inventory_component_rollback_never_initializes_ttt(self):
        from app.services.product_bundles import ProductBundles
        from app.services.brand_inventory import BrandInventory
        from app.services.manual_receipts import ManualReceipts
        sku = SupplyEngine(self.db).resolve_bitrix({'external_product_id':'BUNDLE-HK','name':'Bundle','brand':'Test'})
        ProductBundles(self.db).configure(sku['id'], [{'component_id':self.pid, 'quantity':1}])
        receipts = ManualReceipts(self.db)
        receipt = receipts.create('hong-kong','initial_stock',[{'product_id':self.pid,'quantity':4}])
        receipts.post(receipt['id'])
        with self.db.connect() as c:
            brand_id = c.execute('SELECT brand_id FROM catalog_excel_products WHERE id=?',(self.pid,)).fetchone()[0]
        inventory = BrandInventory(self.db)
        session, _ = inventory.start(brand_id, warehouse_id='hong-kong')
        rows = inventory.list_items(session['id'])
        self.assertEqual(len(rows),1)  # No virtual bundle or TTT-only positions.
        item = rows[0]
        self.assertTrue(item['physical_inventory_initialized'])
        def fail(_):
            raise RuntimeError('rollback fixture')
        with self.assertRaisesRegex(RuntimeError, 'rollback fixture'):
            inventory.confirm(session['id'],item['id'],3,idempotency_key='hk-component',failure_hook=fail)
        self.assertEqual(inventory.list_items(session['id'])[0]['current_stock'],4)
        inventory.confirm(session['id'],item['id'],3,idempotency_key='hk-component')
        with self.db.connect() as c:
            physical = c.execute('SELECT physical_stock,initialized_at FROM erp_component_inventory WHERE product_id=?',(self.pid,)).fetchone()
            self.assertEqual(tuple(physical),(None,None))
            self.assertEqual(balance(c,self.pid,warehouse_id='hong-kong'),3)
            self.assertEqual(c.execute('SELECT stock FROM catalog_excel_products WHERE id=?',(self.pid,)).fetchone()[0],10)

    def test_hk_supply_picker_and_history_never_show_ttt_balance(self):
        from app.services.shared_catalog import SharedCatalog
        from app.services.supplies import SupplyError
        engine = SupplyEngine(self.db)
        # A receipt can introduce a SKU that has never been stocked in HK.
        options = SharedCatalog(self.db).list_products(warehouse_id='hong-kong')
        self.assertEqual(options[0]['stock'],0)
        doc = engine.create('HK arrival', warehouse_id='hong-kong',
                            items=[{'product_id':self.pid,'quantity':3}],key='supply-hk-key')
        self.assertEqual(doc['items'][0]['stock'],0)
        with self.assertRaisesRegex(SupplyError,'другого склада'):
            engine.create('HK arrival',warehouse_id='default',key='supply-hk-key')
        engine.post(doc['id'])
        self.assertEqual(self.stocks(),(10,3))
        movement = next(r for r in engine.movements() if r['source_id']==doc['id'])
        self.assertEqual((movement['warehouse_name'],movement['stock_before'],movement['stock_after']),('Гонконг',0,3))

    def test_existing_wb_sale_can_explicitly_use_hk_without_wb_lifecycle(self):
        from app.services.wildberries_orders import normalize_wildberries_order
        from app.services.wildberries_sales import WildberriesSales
        doc = self.transfers.create('default','hong-kong',[{'product_id':self.pid,'quantity':2}],'wb-hk-fixture')
        self.transfers.transition(doc['id'],'send')
        self.transfers.transition(doc['id'],'receive')
        order = normalize_wildberries_order({'id':124,'article':'MP-1','convertedFinalPrice':123456,'currencyCode':643})
        service = WildberriesSales(self.sales,lambda order:[{'state':'mapped','product':{'id':self.pid,'name':'Demo watch'}}])
        sale = service.conduct(order,warehouse_id='hong-kong')
        self.assertEqual(sale['warehouse_id'],'hong-kong')
        self.assertEqual(self.stocks(),(8,1))
        self.assertEqual(service.conduct(order,warehouse_id='default')['id'],sale['id'])
        self.sales.cancel_sale(sale['id'])
        self.assertEqual(self.stocks(),(8,2))

    def test_hk_catalog_filters_before_limit_and_scopes_facets(self):
        from app.services.shared_catalog import SharedCatalog
        other = SupplyEngine(self.db).resolve_bitrix({
            'external_product_id':'aaa', 'name':'AAA TTT only', 'brand':'Other'})
        self.catalog.update_product(other['id'], stock=99, stock_reason='fixture')
        doc = self.transfers.create('default','hong-kong',[{'product_id':self.pid,'quantity':10}],'hk-only')
        self.transfers.transition(doc['id'],'send')
        self.transfers.transition(doc['id'],'receive')
        catalog = SharedCatalog(self.db)
        products = catalog.list_products(warehouse_id='hong-kong', in_stock=True, limit=1)
        self.assertEqual([int(p['id']) for p in products], [self.pid])
        self.assertEqual(products[0]['stock'], 10)
        self.assertEqual(catalog.count_products(warehouse_id='hong-kong', in_stock=True), 1)
        brands, total = catalog.warehouse_options('brand','hong-kong',in_stock=True)
        self.assertEqual(total, 1)
        self.assertEqual(brands[0]['name'], 'Test')
        with self.assertRaises(ValueError):
            catalog.count_products(warehouse_id='missing', in_stock=True)


if __name__ == '__main__':
    unittest.main()
