"""Synthetic databases and a no-network DOM harness; no live inventory."""
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from app import web
from app.catalog_db import CatalogDatabase
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.warehouse_transfers import WarehouseTransfers
import test_single_bitrix_product_import as import_fixtures


class ProductWarehouseDetailsTest(unittest.TestCase):
    setUp = import_fixtures.SingleBitrixProductImportTest.setUp
    tearDown = import_fixtures.SingleBitrixProductImportTest.tearDown
    taxonomy = import_fixtures.SingleBitrixProductImportTest.taxonomy
    post_import = import_fixtures.SingleBitrixProductImportTest.post_import

    def product(self, quantity=5, warehouse_id='default'):
        response = self.post_import(
            import_fixtures.source_product('901', image=False),
            quantity=quantity, warehouse_id=warehouse_id,
        )
        self.assertIn(response.status_code, (200, 201), response.get_json())
        return int(response.get_json()['data']['erp_product_id'])

    def stocks(self, pid):
        response = self.client.get('/api/v1/products/{}/warehouse-stocks'.format(pid))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return {row['id']: row for row in response.get_json()['data']}

    def movements(self, pid):
        response = self.client.get('/api/v1/products/{}/movements'.format(pid))
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()['data']

    def snapshot(self):
        with CatalogDatabase(self.database_path).connect() as connection:
            return list(connection.iterdump())

    def test_card_lists_both_warehouses_including_zero(self):
        pid = self.product()
        stocks = self.stocks(pid)
        self.assertEqual(list(stocks), ['default', 'hong-kong'])
        self.assertEqual((stocks['default']['quantity'], stocks['hong-kong']['quantity']), (5, 0))
        self.assertTrue(all(row['confirmed'] and row['in_transit'] == 0 for row in stocks.values()))

    def test_hong_kong_import_history_scopes_before_after_to_hong_kong(self):
        pid = self.product(7, 'hong-kong')
        stocks = self.stocks(pid)
        self.assertEqual((stocks['default']['quantity'], stocks['hong-kong']['quantity']), (0, 7))
        movement = next(row for row in self.movements(pid) if row['type'] == 'receipt')
        self.assertEqual((movement['warehouse_id'], movement['warehouse_name']), ('hong-kong', stocks['hong-kong']['name']))
        self.assertEqual((movement['stock_before'], movement['stock_after'], movement['diff']), (0, 7, 7))
        self.assertTrue(movement['document_number'])

    def test_shared_card_shows_receipts_on_both_warehouses(self):
        pid = self.product(5)
        self.assertEqual(self.product(2, 'hong-kong'), pid)
        self.assertEqual({row['warehouse_id'] for row in self.movements(pid) if row['type'] == 'receipt'}, {'default', 'hong-kong'})
        stocks = self.stocks(pid)
        self.assertEqual((stocks['default']['quantity'], stocks['hong-kong']['quantity']), (5, 2))

    def test_transit_and_receipt_have_separate_quantities_and_history(self):
        pid = self.product(5)
        transfers = WarehouseTransfers(CatalogDatabase(self.database_path))
        doc = transfers.create('default', 'hong-kong', [{'product_id': pid, 'quantity': 3}], 'card-transfer')
        transfers.transition(doc['id'], 'send', actor='Tester')
        stocks = self.stocks(pid)
        self.assertEqual((stocks['default']['quantity'], stocks['hong-kong']['quantity'], stocks['hong-kong']['in_transit']), (2, 0, 3))
        transfers.transition(doc['id'], 'receive', actor='Tester')
        stocks = self.stocks(pid)
        self.assertEqual((stocks['default']['quantity'], stocks['hong-kong']['quantity'], stocks['hong-kong']['in_transit']), (2, 3, 0))
        operations = {row['label']: row for row in self.movements(pid) if row['document_number'] == doc['number']}
        sent = operations['Перемещение: отправка']
        received = operations['Перемещение: приёмка']
        self.assertEqual((sent['warehouse_id'], sent['stock_before'], sent['stock_after']), ('default', 5, 2))
        self.assertEqual((received['warehouse_id'], received['stock_before'], received['stock_after']), ('hong-kong', 0, 3))
        self.assertIn('→', sent['reason'])

    def test_cancelled_draft_does_not_appear_as_transit_or_movement(self):
        pid = self.product()
        transfers = WarehouseTransfers(CatalogDatabase(self.database_path))
        doc = transfers.create('default', 'hong-kong', [{'product_id': pid, 'quantity': 3}], 'cancelled')
        transfers.transition(doc['id'], 'cancel')
        self.assertEqual(self.stocks(pid)['hong-kong']['in_transit'], 0)
        self.assertFalse(any(row['document_number'] == doc['number'] for row in self.movements(pid)))

    def test_reads_do_not_change_stock_documents_or_bitrix(self):
        pid = self.product()
        before = self.snapshot()
        with mock.patch.object(web, '_bitrix_single_client', side_effect=AssertionError('External call')):
            for unused in range(2):
                self.stocks(pid)
                self.movements(pid)
        self.assertEqual(before, self.snapshot())

    def test_legacy_manual_and_excel_history_are_ttt(self):
        database = CatalogDatabase(self.database_path)
        with database.connect() as connection:
            pid = connection.execute("SELECT id FROM catalog_excel_products WHERE excel_article='SEED-1'").fetchone()[0]
        ExcelProductCatalog(database).update_product(pid, stock=3, stock_reason='Correction')
        movements = self.movements(pid)
        self.assertIn('initial_stock', {row['type'] for row in movements})
        self.assertIn('manual_adjustment', {row['type'] for row in movements})
        self.assertTrue(all(row['warehouse_id'] == 'default' and row['warehouse_name'] for row in movements))

    def test_photos_have_no_warehouse_or_stock_delta(self):
        pid = self.product()
        photo = {'id': 'photo-test', 'product_id': str(pid), 'type': 'product_photo', 'label': 'Фото', 'created_at': '2099-01-01'}
        with mock.patch.object(web, 'load_stock_operations', return_value=[photo]):
            movement = self.movements(pid)[0]
        self.assertEqual(movement['type'], 'product_photo')
        self.assertNotIn('warehouse_id', movement)

    def test_unconfirmed_component_is_not_shown_as_zero(self):
        pid = self.product(0)
        with CatalogDatabase(self.database_path).transaction() as connection:
            connection.execute('INSERT INTO erp_component_inventory(product_id,updated_at) VALUES(?,?)', (pid, '2026-10-05'))
        stocks = self.stocks(pid)
        self.assertIsNone(stocks['default']['quantity'])
        self.assertFalse(stocks['default']['confirmed'])
        self.assertEqual(stocks['hong-kong']['quantity'], 0)
        self.assertTrue(stocks['hong-kong']['confirmed'])

    def test_disabled_warehouse_with_stock_remains_visible(self):
        pid = self.product(2, 'hong-kong')
        with CatalogDatabase(self.database_path).transaction() as connection:
            connection.execute("UPDATE erp_warehouses SET active=0 WHERE id='hong-kong'")
        stock = self.stocks(pid)['hong-kong']
        self.assertFalse(stock['active'])
        self.assertEqual(stock['quantity'], 2)

    def test_missing_product_is_404_not_zero_stock(self):
        response = self.client.get('/api/v1/products/999999999/warehouse-stocks')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['code'], 'PRODUCT_NOT_FOUND')

    def test_template_scopes_stock_editor_to_selected_warehouse(self):
        template = Path(web.app.root_path, 'templates', 'warehouse.html').read_text(encoding='utf-8')
        self.assertIn('Остатки по складам', template)
        self.assertIn('Остаток выбранного склада', template)
        self.assertIn('ProductWarehouseDetails.appendStockChange(body, stockInput)', template)
        self.assertIn('data-stock="{{ item.ttt_stock|default(item.stock_display)|e }}"', template)
        self.assertIn('renderProductStock(productId);', template)

    def correction(self, pid, warehouse='hong-kong', before=0, after=4, **extra):
        with mock.patch.object(web, 'current_audit_actor', return_value={
            'actor_id': 'tester', 'actor_name': 'Тестовый менеджер', 'actor_type': 'user',
        }):
            return self.client.patch('/api/v1/products/{}'.format(pid), json={
                'stock': after, 'stock_expected': before, 'stock_warehouse_id': warehouse,
                'stock_reason': 'Проверка фактического остатка', **extra,
            })

    def test_hong_kong_correction_preserves_ttt_and_has_scoped_history(self):
        pid = self.product(5)
        response = self.correction(pid)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['data']['stock'], 5)
        stocks = self.stocks(pid)
        self.assertEqual((stocks['default']['quantity'], stocks['hong-kong']['quantity']), (5, 4))
        movement = next(row for row in self.movements(pid) if row['type'] == 'manual_adjustment')
        self.assertEqual((movement['warehouse_id'], movement['stock_before'], movement['stock_after'], movement['diff']), ('hong-kong', 0, 4, 4))
        self.assertEqual(movement['reason'], 'Проверка фактического остатка')
        self.assertEqual(movement['user_name'], 'Тестовый менеджер')

    def test_ttt_correction_and_multipart_use_explicit_warehouse(self):
        pid = self.product(5)
        self.assertEqual(self.correction(pid).status_code, 200)
        response = self.client.patch('/api/products/{}'.format(pid), data={
            'stock': '2', 'stock_expected': '5', 'stock_warehouse_id': 'default',
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_json())
        stocks = self.stocks(pid)
        self.assertEqual((stocks['default']['quantity'], stocks['hong-kong']['quantity']), (2, 4))
        self.assertEqual(len([m for m in self.movements(pid) if m['type'] == 'manual_adjustment']), 2)

    def test_conflict_rolls_back_shared_fields_and_retry_does_not_duplicate(self):
        pid = self.product(5)
        self.assertEqual(self.correction(pid).status_code, 200)
        before = self.snapshot()
        response = self.correction(pid, model='SHOULD-NOT-SAVE')
        self.assertEqual(response.status_code, 422)
        self.assertIn('уже изменился', response.get_json()['message'])
        self.assertEqual(self.snapshot(), before)
        response = self.correction(pid, before=4)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len([m for m in self.movements(pid) if m['type'] == 'manual_adjustment']), 1)

    def test_invalid_scoped_corrections_never_write(self):
        pid = self.product(5)
        before = self.snapshot()
        for change in ({'stock': -1}, {'stock': 1.5}, {'stock': True}, {'stock': 'NaN'},
                       {'stock': ''}, {'stock_expected': None}, {'stock_warehouse_id': 'all'},
                       {'stock_warehouse_id': ''}, {'stock_warehouse_id': 'missing'},
                       {'stock': 2147483648}):
            with self.subTest(change=change):
                response = self.correction(pid, **change)
                self.assertEqual(response.status_code, 422, response.get_json())
                self.assertEqual(self.snapshot(), before)

    def test_disabled_warehouse_and_non_admin_cannot_correct(self):
        pid = self.product(5)
        with mock.patch.object(web, '_manual_product_stock_edit_allowed', return_value=False):
            self.assertEqual(self.correction(pid).status_code, 403)
        with CatalogDatabase(self.database_path).transaction() as c:
            c.execute("UPDATE erp_warehouses SET active=0 WHERE id='hong-kong'")
        before = self.snapshot()
        self.assertEqual(self.correction(pid).status_code, 422)
        self.assertEqual(self.snapshot(), before)

    def test_physical_component_cannot_bypass_existing_correction_guard(self):
        pid = self.product(5)
        with CatalogDatabase(self.database_path).transaction() as c:
            c.execute('INSERT INTO erp_component_inventory(product_id,updated_at) VALUES(?,?)', (pid, '2026-10-06'))
        self.assertTrue(all(not row['editable'] for row in self.stocks(pid).values()))
        before = self.snapshot()
        self.assertEqual(self.correction(pid).status_code, 422)
        self.assertEqual(self.snapshot(), before)

    def test_inventory_lock_and_failed_journal_roll_back_correction(self):
        pid = self.product(5)
        before = self.snapshot()
        with mock.patch('app.services.excel_product_catalog.assert_products_unlocked', side_effect=ValueError('Инвентаризация')):
            self.assertEqual(self.correction(pid).status_code, 422)
        self.assertEqual(self.snapshot(), before)
        with mock.patch('app.services.excel_product_catalog.AuditJournal.record', side_effect=ValueError('Journal failure')):
            self.assertEqual(self.correction(pid).status_code, 422)
        self.assertEqual(self.snapshot(), before)

    def test_correction_does_not_receive_in_transit_stock(self):
        pid = self.product(5)
        transfers = WarehouseTransfers(CatalogDatabase(self.database_path))
        doc = transfers.create('default', 'hong-kong', [{'product_id': pid, 'quantity': 3}], 'correction-transit')
        transfers.transition(doc['id'], 'send')
        self.assertEqual(self.correction(pid).status_code, 200)
        stocks = self.stocks(pid)
        self.assertEqual((stocks['default']['quantity'], stocks['hong-kong']['quantity'], stocks['hong-kong']['in_transit']), (2, 4, 3))
        self.assertEqual(transfers.get(doc['id'])['status'], 'in_transit')

    def test_dom_rendering_safe_text_scopes_transit_errors_and_races(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('product_warehouse_details_node.js'))], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('warehouse detail checks passed', result.stdout)
