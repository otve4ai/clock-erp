import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.catalog_db import CatalogDatabase
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.manual_receipts import ManualReceipts
from app.services.supplies import SupplyEngine
from app.services.component_inventory import balance
from app.services.warehouse_transfers import WarehouseTransfers, TransferError, distribution
from app.services.sales_inventory import SalesInventory


class MultiwarehouseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase(Path(self.temp.name) / 'catalog.db')
        self.db.initialize()
        self.catalog = ExcelProductCatalog(self.db)
        self.pid = int(SupplyEngine(self.db).resolve_bitrix({'external_product_id': 'MW-1', 'name': 'Test watch', 'external_sku': 'MW-1', 'brand': 'Test'})['id'])
        self.catalog.update_product(self.pid, stock=10, stock_reason='initial')
        self.transfers = WarehouseTransfers(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def stock(self, wid='default'):
        with self.db.connect() as c:
            return balance(c, self.pid, warehouse_id=wid)

    def transfer(self, quantity=4, key='transfer-1'):
        return self.transfers.create('default', 'hong-kong', [{'product_id': self.pid, 'quantity': quantity}], key, 'tester')

    def test_transit_and_retry_preserve_company_quantity(self):
        doc = self.transfer()
        self.assertEqual(self.stock(), 10)
        self.transfers.transition(doc['id'], 'send')
        self.transfers.transition(doc['id'], 'send')
        self.assertEqual((self.stock(), self.stock('hong-kong')), (6, 0))
        with self.db.connect() as c:
            rows = distribution(c, [self.pid])[self.pid]
            self.assertEqual(sum(r['quantity'] + r['in_transit'] for r in rows), 10)
        self.transfers.transition(doc['id'], 'receive')
        self.transfers.transition(doc['id'], 'receive')
        self.assertEqual((self.stock(), self.stock('hong-kong')), (6, 4))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM catalog_stock_movements WHERE source_type='transfer'").fetchone()[0], 2)

    def test_invalid_receive_and_cancel_do_not_move_stock(self):
        for pid, quantity in [(True,1),(1.5,1),(self.pid,True),(self.pid,float('inf')),(self.pid,0.5)]:
            with self.subTest(pid=pid, quantity=quantity), self.assertRaises(TransferError):
                self.transfers.create('default','hong-kong',[{'product_id':pid,'quantity':quantity}],'invalid')
        doc = self.transfer()
        with self.assertRaises(TransferError):
            self.transfers.transition(doc['id'], 'receive')
        self.transfers.transition(doc['id'], 'send')
        with self.assertRaises(TransferError):
            self.transfers.transition(doc['id'], 'cancel')
        self.assertEqual(self.stock(), 6)

    def test_duplicate_key_mismatch_is_rejected(self):
        first = self.transfer()
        self.assertEqual(first['id'], self.transfer()['id'])
        with self.assertRaises(TransferError):
            self.transfer(quantity=5)

    def test_concurrent_send_only_once(self):
        doc = self.transfer()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.transfers.transition(doc['id'], 'send'), range(2)))
        self.assertEqual(self.stock(), 6)

    def test_failure_rolls_back_stock_and_document(self):
        doc = self.transfer()
        def fail(c):
            raise RuntimeError('synthetic failure')
        with self.assertRaises(RuntimeError):
            self.transfers.transition(doc['id'], 'send', failure_hook=fail)
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.transfers.get(doc['id'])['status'], 'draft')
        empty = int(SupplyEngine(self.db).resolve_bitrix({'external_product_id':'MW-empty', 'name':'Empty', 'brand':'Test'})['id'])
        multiple = self.transfers.create('default', 'hong-kong', [{'product_id':self.pid,'quantity':1},{'product_id':empty,'quantity':1}], 'multiple')
        with self.assertRaises(TransferError):
            self.transfers.transition(multiple['id'], 'send')
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.transfers.get(multiple['id'])['status'], 'draft')

    def test_hong_kong_receipt_does_not_change_ttt(self):
        engine = ManualReceipts(self.db)
        doc = engine.create('hong-kong', 'initial_stock', [{'product_id': self.pid, 'quantity': 25}])
        engine.post(doc['id'])
        engine.post(doc['id'])
        self.assertEqual((self.stock(), self.stock('hong-kong')), (10, 25))
        engine.cancel(doc['id'])
        self.assertEqual((self.stock(), self.stock('hong-kong')), (10, 0))

    def test_sale_and_cancel_use_original_warehouse(self):
        doc = self.transfer()
        self.transfers.transition(doc['id'], 'send')
        self.transfers.transition(doc['id'], 'receive')
        sales = SalesInventory(self.db)
        sale = sales.create_sale({'source': 'Amazon', 'warehouse_id': 'hong-kong'}, self.pid, 2, 100)
        self.assertEqual((self.stock(), self.stock('hong-kong')), (6, 2))
        sales.cancel_sale(sale['id'], reason='test', user_name='tester')
        self.assertEqual((self.stock(), self.stock('hong-kong')), (6, 4))

    def test_ttt_sale_cannot_spend_transit(self):
        doc = self.transfer(quantity=10)
        self.transfers.transition(doc['id'], 'send')
        with self.assertRaises(ValueError):
            SalesInventory(self.db).create_sale({'source': 'Tictactoy'}, self.pid, 1, 100)
        self.assertEqual(self.stock(), 0)

    def test_direct_legacy_edit_is_ttt_only(self):
        doc = self.transfer()
        self.transfers.transition(doc['id'], 'send')
        self.transfers.transition(doc['id'], 'receive')
        self.catalog.update_product(self.pid, stock=7, stock_reason='count')
        self.assertEqual((self.stock(), self.stock('hong-kong')), (7, 4))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT quantity FROM erp_warehouse_stocks WHERE product_id=? AND warehouse_id='default'", (self.pid,)).fetchone()[0], 7)

    def test_supply_receipt_uses_destination(self):
        engine = SupplyEngine(self.db)
        doc = engine.create('Test', items=[{'product_id': self.pid, 'quantity': 7}], warehouse_id='hong-kong')
        engine.post(doc['id'])
        self.assertEqual((self.stock(), self.stock('hong-kong')), (10, 7))

    def test_supply_add_and_delete_ignore_sales_on_other_warehouse(self):
        engine = SupplyEngine(self.db)
        doc = engine.create('Test', items=[{'product_id': self.pid, 'quantity': 7}], warehouse_id='hong-kong')
        engine.post(doc['id'])
        engine.add_item(doc['id'], self.pid, 2, 'addition-key')
        SalesInventory(self.db).create_sale({'source':'Tictactoy'}, self.pid, 1, 100)
        engine.delete(doc['id'])
        self.assertEqual((self.stock(), self.stock('hong-kong')), (9, 0))

    def test_hk_batch_return_and_retry_never_touch_ttt(self):
        doc = self.transfer(quantity=10)
        self.transfers.transition(doc['id'], 'send')
        self.transfers.transition(doc['id'], 'receive')
        sales = SalesInventory(self.db)
        payload = {'source':'Amazon', 'warehouse_id':'hong-kong', 'order_number':'AMZ-1'}
        items = [{'product_id':self.pid, 'quantity':2, 'unit_price':100}]
        sale = sales.create_sale_batch(payload, items, idempotency_key='amazon-event-1', enforce_external_unique=True)
        sales.create_sale_batch(payload, items, idempotency_key='amazon-event-1', enforce_external_unique=True)
        self.assertEqual((self.stock(), self.stock('hong-kong')), (0, 8))
        sales.return_sale(sale['id'], quantity=1, reason='test')
        self.assertEqual((self.stock(), self.stock('hong-kong')), (0, 9))
        sales.cancel_sale(sale['id'], reason='test')
        self.assertEqual((self.stock(), self.stock('hong-kong')), (0, 10))

    def test_transit_not_in_available_filter_and_cannot_delete_card(self):
        doc = self.transfer(quantity=10)
        self.transfers.transition(doc['id'], 'send')
        self.assertEqual(self.catalog.list_products(warehouse_id='all', stock_state='in')['total'], 0)
        self.assertEqual(self.catalog.list_products(warehouse_id='all')['stats']['positive_positions'], 0)
        with self.assertRaises(ValueError):
            self.catalog.delete_product(self.pid, force=True)
        self.transfers.transition(doc['id'], 'receive')
        with self.assertRaises(ValueError):
            self.catalog.archive_product(self.pid)
        with self.assertRaises(ValueError):
            self.catalog.delete_product(self.pid, force=True)

    def test_transfer_http_csrf_readonly_and_retries(self):
        from app import web
        from app.auth import require_csrf
        with patch.dict(web.app.config, TESTING=True, AUTH_TESTING=False), patch.dict('os.environ', {'CATALOG_DATABASE_PATH':str(self.db.path)}):
            client = web.app.test_client()
            body = {'from_warehouse_id':'default','to_warehouse_id':'hong-kong','items':[{'product_id':self.pid,'quantity':3}]}
            with patch('app.supply_routes.require_csrf_when_authenticated', side_effect=require_csrf):
                self.assertEqual(client.post('/api/v1/receipts/transfers', json=body).status_code, 403)
                with client.session_transaction() as session:
                    session['_csrf_token'] = 'synthetic-csrf'
                headers = {'X-CSRF-Token':'synthetic-csrf','Idempotency-Key':'hk-transfer-1'}
                response = client.post('/api/v1/receipts/transfers', json=body, headers=headers)
                self.assertEqual(response.status_code, 201, response.data)
                doc = response.get_json()['data']
                self.assertEqual(client.post('/api/v1/receipts/transfers', json=body, headers=headers).get_json()['data']['id'], doc['id'])
            with patch('app.supply_routes.auth_is_enabled', return_value=True), patch('app.supply_routes.current_auth_user', return_value={'role':'viewer'}):
                self.assertEqual(client.post('/api/v1/receipts/transfers/'+doc['id']+'/send', json={}).status_code, 403)
            self.assertEqual(self.stock(), 10)

    def test_writeoff_and_cancel_use_hk_and_ledger_scope(self):
        from app.services.writeoffs import Writeoffs
        doc = self.transfer()
        self.transfers.transition(doc['id'], 'send')
        self.transfers.transition(doc['id'], 'receive')
        engine = Writeoffs(self.db)
        payload = {'product_id':self.pid, 'quantity':2, 'reason':'Брак', 'warehouse_id':'hong-kong'}
        writeoff = engine.create(payload, {}, 'hk-writeoff')
        engine.create(payload, {}, 'hk-writeoff')
        self.assertEqual((self.stock(), self.stock('hong-kong')), (6, 2))
        engine.cancel(writeoff['id'], {})
        self.assertEqual((self.stock(), self.stock('hong-kong')), (6, 4))
        with self.db.connect() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT DISTINCT warehouse_id FROM catalog_stock_movements WHERE source_type='writeoff'")], ['hong-kong'])

    def test_inventory_ttt_posts_delta_without_touching_hk(self):
        from app.services.brand_inventory import BrandInventory
        doc = self.transfer()
        self.transfers.transition(doc['id'], 'send')
        self.transfers.transition(doc['id'], 'receive')
        with self.db.connect() as c:
            brand = c.execute('SELECT brand_id FROM catalog_excel_products WHERE id=?', (self.pid,)).fetchone()[0]
        engine = BrandInventory(self.db)
        session, created = engine.start(brand)
        with self.db.connect() as c:
            item_id = c.execute('SELECT id FROM erp_inventory_items WHERE session_id=? AND product_id=?', (session['id'], self.pid)).fetchone()[0]
        engine.confirm(session['id'], item_id, 8, idempotency_key='count-ttt')
        engine.confirm(session['id'], item_id, 8, idempotency_key='count-ttt')
        self.assertEqual((self.stock(), self.stock('hong-kong')), (8, 4))
        with self.db.connect() as c:
            rows = c.execute("SELECT quantity_delta,warehouse_id FROM catalog_stock_movements WHERE source_type='inventory'").fetchall()
            self.assertEqual([tuple(r) for r in rows], [(2, 'default')])

    def test_warehouse_filter_counts_and_sorting_before_pagination(self):
        doc = self.transfer()
        self.transfers.transition(doc['id'], 'send')
        result = self.catalog.list_products(warehouse_id='hong-kong')
        self.assertEqual(result['total'], 1)  # Inbound transit makes it relevant.
        self.assertEqual(result['items'][0]['stock'], 0)
        self.assertEqual(result['stats']['total_stock'], 0)
        result = self.catalog.list_products(warehouse_id='all')
        self.assertEqual(result['items'][0]['stock'], 10)
        self.transfers.transition(doc['id'], 'receive')
        result = self.catalog.list_products(warehouse_id='hong-kong', stock_state='in', sort_by='stock', per_page=1)
        self.assertEqual(result['stats']['total_stock'], 4)
        self.assertEqual(result['items'][0]['stock'], 4)
        with self.assertRaises(ValueError):
            self.catalog.list_products(warehouse_id='wb')

    def test_products_and_transfer_page_render_without_external_services(self):
        from app import web
        with patch.dict(web.app.config, TESTING=True, AUTH_TESTING=False), patch.dict('os.environ', {'CATALOG_DATABASE_PATH': str(self.db.path)}):
            client = web.app.test_client()
            with patch('app.web.ExcelProductCatalog', return_value=self.catalog):
                response = client.get('/app/products?warehouse_id=hong-kong')
            self.assertEqual(response.status_code, 200, response.data[:1000])
            self.assertIn('warehouseSelector', response.text)
            self.assertIn('На сайте TTT', response.text)
            self.assertNotIn('value="wb"', response.text)
            response = client.get('/warehouse/transfers')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('value="wb"', response.text)


class MultiwarehouseMigrationTest(unittest.TestCase):
    def test_existing_channel_code_requires_explicit_reconciliation(self):
        import sqlite3
        import app.schema_migrations as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            with patch.object(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:-1]), patch.object(migrations, 'verify_complete_catalog_contract', return_value=True):
                migrations.apply_migrations(path)
                with sqlite3.connect(str(path)) as c:
                    c.execute("INSERT INTO erp_warehouses VALUES('existing-hk','HK','Гонконг',1,0,'2026-01-01','2026-01-01')")
            with self.assertRaisesRegex(ValueError, 'согласовать существующий склад HK'):
                migrations.apply_migrations(path)
            with sqlite3.connect(str(path)) as c:
                self.assertNotIn('warehouse_id', [r[1] for r in c.execute('PRAGMA table_info(erp_sales)')])
                self.assertEqual(c.execute("SELECT id FROM erp_warehouses WHERE code='HK'").fetchone()[0], 'existing-hk')

    def test_existing_split_is_reconciled_once_and_corruption_aborts(self):
        import sqlite3
        import app.schema_migrations as migrations
        for other in (4, 12):
            with self.subTest(other=other), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'legacy.db'
                with patch.object(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:-1]), patch.object(migrations, 'verify_complete_catalog_contract', return_value=True):
                    migrations.apply_migrations(path)
                    db = CatalogDatabase(path)
                    pid = int(SupplyEngine(db).resolve_bitrix({'external_product_id':'old', 'name':'Old stock', 'brand':'Test'})['id'])
                    ExcelProductCatalog(db).update_product(pid, stock=10, stock_reason='initial')
                    with sqlite3.connect(str(path)) as c:
                        c.execute("INSERT INTO erp_warehouses VALUES('old-hk','OTHER','Другой склад',1,0,'2026-01-01','2026-01-01')")
                        c.execute("INSERT INTO erp_warehouse_stocks VALUES('old-hk',?,?,'2026-01-01')", (pid,other))
                if other > 10:
                    with self.assertRaises(ValueError):
                        migrations.apply_migrations(path)
                    with sqlite3.connect(str(path)) as c:
                        self.assertEqual(c.execute('SELECT stock FROM catalog_excel_products WHERE id=?', (pid,)).fetchone()[0], 10)
                        self.assertNotIn('warehouse_id', [r[1] for r in c.execute('PRAGMA table_info(erp_sales)')])
                else:
                    with sqlite3.connect(str(path)) as c:
                        before = migrations.business_snapshot(c)
                    migrations.apply_migrations(path)
                    migrations.apply_migrations(path)
                    with sqlite3.connect(str(path)) as c:
                        self.assertEqual(c.execute('SELECT stock FROM catalog_excel_products WHERE id=?', (pid,)).fetchone()[0], 6)
                        self.assertEqual(c.execute('SELECT SUM(quantity) FROM erp_warehouse_stocks WHERE product_id=?', (pid,)).fetchone()[0], 10)
                        self.assertEqual(migrations.business_snapshot(c), before)

    def test_upgrade_preserves_stock_and_is_repeatable(self):
        import sqlite3
        import app.schema_migrations as migrations
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            registry = migrations.MIGRATIONS
            with patch.object(migrations, 'MIGRATIONS', registry[:-1]), patch.object(migrations, 'verify_complete_catalog_contract', return_value=True):
                migrations.apply_migrations(path)
                db = CatalogDatabase(path)
                pid = int(SupplyEngine(db).resolve_bitrix({'external_product_id': 'migration-1', 'name': 'Test', 'brand': 'Test'})['id'])
                ExcelProductCatalog(db).update_product(pid, stock=10, stock_reason='initial')
                with sqlite3.connect(str(path)) as c:
                    c.execute("INSERT INTO erp_sales(id,source,status,created_at,inserted_at,updated_at) VALUES('historical','tictactoy','completed','2026-01-01','2026-01-01','2026-01-01')")
            migrations.apply_migrations(path)
            migrations.apply_migrations(path)
            with sqlite3.connect(str(path)) as c:
                self.assertEqual(c.execute('SELECT stock FROM catalog_excel_products WHERE id=?', (pid,)).fetchone()[0], 10)
                self.assertEqual(c.execute('SELECT warehouse_id,quantity FROM erp_warehouse_stocks WHERE product_id=?', (pid,)).fetchall(), [('default',10)])
                self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(c.execute("SELECT warehouse_id FROM erp_sales WHERE id='historical'").fetchone()[0], 'default')
