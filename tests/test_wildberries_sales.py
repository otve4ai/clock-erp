"""WB posting uses real isolated SQLite and never an external integration."""
import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from app import web
from app.catalog_db import CatalogDatabase
from app.schema_migrations import apply_migrations
from app.services.audit_journal import AuditJournal
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.sales_inventory import SalesInventory, SalesInventoryError
from app.services.shared_catalog import SharedCatalog
from app.services.required_straps import RequiredStraps
from app.services.wildberries_orders import normalize_wildberries_order
from app.services.wildberries_sales import WildberriesSales


class WildberriesSalesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = CatalogDatabase(Path(self.temp.name) / 'catalog.db')
        apply_migrations(self.database.path)
        with self.database.transaction() as connection:
            connection.execute("INSERT INTO catalog_excel_batches (id,file_sha256,source_filename,row_count,total_stock,positive_rows,zero_rows,status,created_at,applied_at) VALUES ('wb','wb','test.xlsx',0,0,0,0,'active','2026-09-07','2026-09-07')")
        self.catalog = ExcelProductCatalog(self.database)
        self.watch = self.catalog.create_product('Часы WB', article='WB-1', brand='Brand', category='Часы', stock=3)
        self.shared = SharedCatalog(self.database)
        self.inventory = SalesInventory(self.database)
        self.order = normalize_wildberries_order({'id': 123, 'nmId': 77, 'article': 'WB-1', 'skus': ['4601'], 'convertedFinalPrice': 123456, 'currencyCode': 643})
        self.service = WildberriesSales(self.inventory, self.resolve)

    def resolve(self, order):
        context = web.build_order_product_mapping_context(order['products'], catalog=self.shared)
        return [web.get_order_product_mapping(context, item) for item in order['products']]

    def effects(self):
        with self.database.connect() as connection:
            return tuple(connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('erp_sales', 'erp_sale_items', 'catalog_stock_movements', 'erp_audit_events'))

    def stock(self, product=None):
        return self.catalog.get_product((product or self.watch)['id'])['stock']

    def test_success_price_source_mapping_stock_link_and_journal(self):
        sale = self.service.conduct(self.order, 'Тест')
        self.assertEqual(sale['source'], 'wildberries')
        self.assertEqual(str(sale['product_id']), str(self.watch['id']))
        self.assertEqual(float(sale['unit_price']), 1234.56)
        self.assertEqual(sale['commission'], '')
        self.assertEqual(self.stock(), 2)
        self.assertEqual(self.service.find_sale('123')['id'], sale['id'])
        with self.database.connect() as connection:
            row = connection.execute('SELECT source,external_order_id,idempotency_key FROM erp_sales').fetchone()
            self.assertEqual(tuple(row), ('wildberries', '123', 'wildberries-order:123'))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM erp_audit_events WHERE entity_type='sale'").fetchone()[0], 1)

    def test_repeated_and_refreshed_order_does_not_repeat_effects(self):
        first = self.service.conduct(self.order)
        effects = self.effects()
        refreshed = copy.deepcopy(self.order)
        refreshed['products'][0]['article'] = 'now-unmatched'
        self.assertEqual(self.service.conduct(refreshed)['id'], first['id'])
        self.assertEqual(self.effects(), effects)
        self.assertEqual(self.stock(), 2)

    def test_concurrent_requests_create_one_sale(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            sales = list(pool.map(lambda _: self.service.conduct(self.order), range(2)))
        self.assertEqual(sales[0]['id'], sales[1]['id'])
        self.assertEqual(self.stock(), 2)
        self.assertEqual(self.effects()[0:3], (1, 1, 1))

    def test_unmatched_and_ambiguous_products_have_no_effect(self):
        before = self.effects()
        self.order['products'][0]['article'] = 'unknown'
        with self.assertRaisesRegex(SalesInventoryError, 'Не удалось определить товар ERP'):
            self.service.conduct(self.order)
        self.order['products'][0]['article'] = 'WB-1'
        # Model permits historical duplicates; simulate an existing ambiguous catalogue.
        other = self.catalog.create_product('Другие часы', article='WB-2', brand='Brand', category='Часы', stock=2)
        with self.database.transaction() as connection:
            connection.execute("UPDATE catalog_excel_products SET excel_article='WB-1' WHERE id=?", (other['id'],))
        with self.assertRaisesRegex(SalesInventoryError, 'Не удалось определить товар ERP'):
            self.service.conduct(self.order)
        self.assertEqual(self.effects()[0:3], before[0:3])
        self.assertEqual(self.stock(), 3)

    def test_current_stock_is_checked_and_all_lines_rollback(self):
        extra = copy.deepcopy(self.order['products'][0])
        extra['order_item_id'] = 'second'
        extra['quantity'] = 3
        self.order['products'].append(extra)
        before = self.effects()
        with self.assertRaisesRegex(SalesInventoryError, 'Недостаточно'):
            self.service.conduct(self.order)
        self.assertEqual(self.stock(), 3)
        self.assertEqual(self.effects(), before)

    def test_journal_failure_rolls_back_sale_items_stock_and_movements(self):
        before = self.effects()
        with mock.patch.object(AuditJournal, 'record', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                self.service.conduct(self.order)
        self.assertEqual(self.stock(), 3)
        self.assertEqual(self.effects(), before)

    def test_multiple_positions_share_one_sale(self):
        extra = copy.deepcopy(self.order['products'][0])
        extra['order_item_id'] = 'second'
        self.order['products'].append(extra)
        self.service.conduct(self.order)
        self.assertEqual(self.stock(), 1)
        self.assertEqual(self.effects()[0:3], (1, 2, 2))

    def test_watch_without_enabled_setting_cannot_start_strap_replacement(self):
        base = self.catalog.create_product(
            'Часы основа', article='BASE-WATCH', brand='Brand',
            category='Часы', stock=1,
        )
        strap = self.catalog.create_product(
            'Ремешок', article='STRAP-WATCH', brand='Brand',
            category='Ремешки', stock=1,
        )

        with self.assertRaisesRegex(
            SalesInventoryError,
            'ремешок не включён в настройках',
        ):
            self.service.conduct(self.order, replacement={
                'line_index': 0,
                'base_product_id': base['id'],
                'installed_strap_product_id': strap['id'],
                'removed_strap_mode': 'none',
            })

        self.assertEqual(self.stock(), 3)
        self.assertEqual(self.stock(base), 1)
        self.assertEqual(self.stock(strap), 1)
        self.assertEqual(self.effects()[0:3], (0, 0, 0))

    def test_non_watch_order_cannot_start_strap_replacement(self):
        glasses = self.catalog.create_product(
            'Очки дизайнерские', article='GLASSES-1', brand='Brand',
            category='Очки', stock=1,
        )
        self.order['products'][0]['article'] = 'GLASSES-1'
        base = self.catalog.create_product(
            'Часы основа', article='BASE-GLASSES', brand='Brand',
            category='Часы', stock=1,
        )
        strap = self.catalog.create_product(
            'Ремешок', article='STRAP-GLASSES', brand='Brand',
            category='Ремешки', stock=1,
        )

        with self.assertRaisesRegex(
            SalesInventoryError,
            'ремешок не включён в настройках',
        ):
            self.service.conduct(self.order, replacement={
                'line_index': 0,
                'base_product_id': base['id'],
                'installed_strap_product_id': strap['id'],
                'removed_strap_mode': 'none',
            })

        self.assertEqual(self.stock(glasses), 1)
        self.assertEqual(self.stock(base), 1)
        self.assertEqual(self.stock(strap), 1)
        self.assertEqual(self.effects()[0:3], (0, 0, 0))

    def test_sale_dialog_hides_strap_replacement_for_non_watch(self):
        glasses = self.catalog.create_product(
            'Очки дизайнерские', article='GLASSES-UI', brand='Brand',
            category='Очки', stock=1,
        )
        self.order['products'][0]['article'] = 'GLASSES-UI'
        context = web.build_order_product_mapping_context(
            self.order['products'], catalog=self.shared,
        )

        summary = web.build_order_sale_dialog_summary(
            self.order['products'], mapping_context=context,
        )

        self.assertFalse(summary['lines'][0]['requires_strap'])
        self.assertFalse(summary['lines'][0]['strap_flow_enabled'])
        with self.route_context(), mock.patch.object(
            web, 'get_orders', return_value=[self.order]
        ), mock.patch.object(
            web, 'SharedCatalog', return_value=self.shared
        ), mock.patch.object(
            web, 'load_order_product_mappings', return_value={}
        ), mock.patch.object(web, 'load_stock_operations', return_value=[]):
            page = web.app.test_client().get('/order/wildberries/123')

        self.assertEqual(page.status_code, 200)
        self.assertNotIn('data-open-strap-replacement>', page.get_data(as_text=True))

        RequiredStraps(self.database).configure(glasses['id'], True)
        with self.route_context(), mock.patch.object(
            web, 'get_orders', return_value=[self.order]
        ), mock.patch.object(
            web, 'SharedCatalog', return_value=self.shared
        ), mock.patch.object(
            web, 'load_order_product_mappings', return_value={}
        ), mock.patch.object(web, 'load_stock_operations', return_value=[]):
            reopened = web.app.test_client().get('/order/wildberries/123')

        enabled_html = reopened.get_data(as_text=True)
        self.assertIn('name="required_strap_0_product_id"', enabled_html)
        self.assertNotIn('data-open-strap-replacement>', enabled_html)

    def test_invalid_price_or_currency_does_not_create_sale(self):
        for price, currency in ((None, 643), (-1, 643), (123, 840)):
            with self.subTest(price=price, currency=currency):
                self.order['products'][0]['price'] = price
                self.order['currency_code'] = currency
                with self.assertRaises(ValueError):
                    self.service.conduct(self.order)
        self.assertEqual(self.effects()[0:3], (0, 0, 0))

    def test_cancelled_sale_cannot_be_posted_again(self):
        sale = self.service.conduct(self.order)
        with self.database.transaction() as connection:
            connection.execute("UPDATE erp_sales SET cancelled_at='2026-09-07' WHERE id=?", (sale['id'],))
        self.assertEqual(self.service.conduct(self.order)['id'], sale['id'])
        self.assertEqual(self.stock(), 2)

    def route_context(self):
        stack = ExitStack()
        stack.enter_context(mock.patch.dict(web.app.config, TESTING=True, AUTH_TESTING=False))
        stack.enter_context(mock.patch.object(web, 'OrdersSnapshotStore', return_value=mock.Mock(get=mock.Mock(return_value=self.order))))
        stack.enter_context(mock.patch.object(web, 'SalesInventory', return_value=self.inventory))
        stack.enter_context(mock.patch.object(web, 'resolve_wildberries_sale_products', side_effect=self.resolve))
        stack.enter_context(mock.patch.object(web, 'update_order_status', side_effect=AssertionError('external write')))
        stack.enter_context(mock.patch.object(web, 'synchronize_wildberries_orders', side_effect=AssertionError('external sync')))
        return stack

    def test_endpoint_returns_json_ignores_client_price_and_handles_retry(self):
        with self.route_context():
            client = web.app.test_client()
            response = client.post('/order/wildberries/123/conduct-sale', headers={'Accept': 'application/json'}, data={'original_price_0': 1, 'commission': '50'})
            self.assertEqual(response.status_code, 200)
            again = client.post('/order/wildberries/123/conduct-sale', headers={'Accept': 'application/json'})
        self.assertEqual(
            response.json['stock_notification']['items'][0]['stock_before'], 3,
        )
        self.assertEqual(
            response.json['stock_notification']['items'][0]['stock_after'], 2,
        )
        self.assertIsNone(again.json['stock_notification'])
        self.assertEqual(again.json['sale_id'], response.json['sale_id'])
        self.assertIn('уже проведён', again.json['message'])
        self.assertEqual(self.stock(), 2)

    def test_endpoint_returns_safe_error_and_no_effects(self):
        with self.route_context(), mock.patch.object(AuditJournal, 'record', side_effect=RuntimeError('private details')):
            response = web.app.test_client().post('/order/wildberries/123/conduct-sale', headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('private details', response.json['message'])
        self.assertEqual(self.stock(), 3)

    def test_sale_state_survives_refresh_and_wb_status_changes(self):
        sale = self.service.conduct(self.order)
        self.order['status'] = 'confirm'
        state = web.build_order_sale_state(self.order, {}, self.service.find_sale('123'))
        self.assertTrue(state['sale_completed'])
        self.assertFalse(state['can_create_sale'])
        self.assertEqual(state['sale_id'], sale['id'])

    def test_manual_mapping_response_keeps_wb_sale_ready_without_refresh(self):
        self.order['products'][0]['article'] = 'arma_02'
        endpoint = '/api/orders/{}/items/{}/mapping'.format(
            self.order['id'],
            web.bitrix_order_product_identity(self.order['products'][0])['bitrix_order_line_id'],
        )
        with self.route_context(), mock.patch.object(web, 'CatalogDatabase', return_value=self.database), mock.patch.object(web, 'SharedCatalog', return_value=self.shared):
            client = web.app.test_client()
            for status in ('new', 'confirm', 'complete'):
                with self.subTest(status=status):
                    self.order['status'] = status
                    response = client.post(endpoint, json={'product_id': self.watch['id']})
                    self.assertEqual(response.status_code, 200)
                    state = response.json['data']['order']
                    self.assertTrue(state['all_items_mapped'])
                    self.assertTrue(state['sale_ready'], state['issues'])
                    self.assertEqual(state['issues'], [])
            removed = client.delete(endpoint)
            self.assertEqual(removed.status_code, 200)
            self.assertFalse(removed.json['data']['order']['sale_ready'])
            self.assertIn('Есть несопоставленные позиции', removed.json['data']['order']['issues'])
        self.assertEqual(self.stock(), 3)
        self.assertEqual(self.effects()[0], 0)

    def test_wb_readiness_preserves_stock_and_duplicate_guards(self):
        context = web.build_order_product_mapping_context(self.order['products'], catalog=self.shared)
        selected = web.get_order_product_mapping(context, self.order['products'][0])['product']
        selected['stock'] = 0
        self.assertIn('Недостаточно остатка', web.build_order_sale_readiness(self.order, context)['issues'])
        selected['stock'] = 3
        self.assertFalse(web.build_order_sale_readiness(self.order, context, already_conducted=True)['ready'])
        bitrix_order = dict(self.order, source='tictactoy', status='N')
        self.assertIn('Заказ не подтверждён', web.build_order_sale_readiness(bitrix_order, context)['issues'])

    def map_variant(self, order=None, product=None):
        order = order or self.order
        product = product or self.watch
        with self.route_context(), mock.patch.object(web, 'CatalogDatabase', return_value=self.database), mock.patch.object(web, 'SharedCatalog', return_value=self.shared), mock.patch.object(web, 'OrdersSnapshotStore', return_value=mock.Mock(get=mock.Mock(return_value=order))):
            response = web.app.test_client().post(
                '/api/orders/{}/items/{}/mapping'.format(order['id'], order['products'][0]['order_item_id']),
                json={'product_id': product['id']},
            )
        self.assertEqual(response.status_code, 200)
        return response

    def test_remembers_variant_for_new_order_and_conducts_selected_product(self):
        self.order['products'][0].update(article='arma_02', chrt_id=88)
        self.map_variant()
        next_order = copy.deepcopy(self.order)
        next_order['id'] = 'wb:124'
        next_order['products'][0]['order_item_id'] = '124'
        context = self.resolve(next_order)
        self.assertEqual(context[0]['mapping_method'], 'remembered_wb')
        self.assertEqual(str(context[0]['product']['id']), str(self.watch['id']))
        from app.services.wildberries_matching import order_product_candidates
        with self.database.connect() as connection:
            candidates, method = order_product_candidates(connection, next_order['products'][0])
        self.assertEqual(method, 'remembered_wb')
        self.assertEqual(str(candidates[0]['id']), str(self.watch['id']))
        self.assertEqual(web.load_order_product_mappings('wb:124', self.database), {})
        sale = self.service.conduct(next_order)
        self.assertEqual(str(sale['product_id']), str(self.watch['id']))
        self.assertEqual(self.stock(), 2)

    def test_variant_memory_does_not_cross_size_or_card_and_manual_wins(self):
        self.order['products'][0].update(article='unmatched', chrt_id=88)
        self.map_variant()
        for change in ({'chrt_id': 89}, {'nm_id': 78}, {'source': 'tictactoy'}):
            other = copy.deepcopy(self.order)
            other['products'][0].update(change)
            self.assertNotEqual(self.resolve(other)[0]['state'], 'mapped')
        replacement = self.catalog.create_product('Other variant', article='OTHER', brand='Brand', category='Ремни', stock=2)
        newer = copy.deepcopy(self.order)
        newer['id'] = 'wb:124'
        newer['products'][0]['order_item_id'] = '124'
        self.map_variant(newer, replacement)
        self.assertEqual(str(self.resolve(newer)[0]['product']['id']), str(replacement['id']))
        original = web.build_order_product_mapping_context(self.order['products'],
            mappings=web.load_order_product_mappings(self.order['id'], self.database), catalog=self.shared)
        self.assertEqual(str(web.get_order_product_mapping(original, self.order['products'][0])['product']['id']), str(self.watch['id']))

    def test_barcode_memory_and_weak_identity_stays_order_local(self):
        self.order['products'][0]['article'] = 'unmatched'
        self.map_variant()
        self.assertEqual(self.resolve(self.order)[0]['mapping_method'], 'remembered_wb')
        different = copy.deepcopy(self.order)
        different['products'][0].update(barcode='other', sku='other', skus=['other'])
        self.assertNotEqual(self.resolve(different)[0]['state'], 'mapped')
        weak = copy.deepcopy(self.order)
        weak['products'][0].update(nm_id=99, barcode='', sku='', skus=[])
        self.map_variant(weak)
        self.assertNotEqual(self.resolve(weak)[0]['state'], 'mapped')

    def test_remembered_archived_product_does_not_fall_back_to_article(self):
        self.order['products'][0]['article'] = 'unmatched'
        self.map_variant()
        with self.database.transaction() as connection:
            connection.execute('UPDATE catalog_excel_products SET active=0 WHERE id=?', (self.watch['id'],))
        self.catalog.create_product('Wrong fallback', article='unmatched', brand='Brand', category='Ремни', stock=2)
        self.assertEqual(self.resolve(self.order)[0]['state'], 'archived')
        with self.assertRaises(SalesInventoryError):
            self.service.conduct(self.order)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute('SELECT stock FROM catalog_excel_products WHERE id=?', (self.watch['id'],)).fetchone()[0], 3)

    def test_mapping_memory_and_order_choice_are_atomic(self):
        from app.services import wildberries_product_mappings
        import sqlite3
        with mock.patch.object(wildberries_product_mappings, 'remember_product', side_effect=sqlite3.OperationalError('test rollback')):
            with self.assertRaises(sqlite3.OperationalError):
                web.save_order_product_mapping(self.order['id'], '123', self.watch['id'], self.database,
                                               wb_product=self.order['products'][0])
        self.assertEqual(web.load_order_product_mappings(self.order['id'], self.database), {})

    def test_remembered_mapping_can_be_cleared_from_another_order(self):
        self.order['products'][0]['article'] = 'unmatched'
        self.map_variant()
        self.order['id'] = 'wb:124'
        self.order['products'][0]['order_item_id'] = '124'
        with self.route_context(), mock.patch.object(web, 'CatalogDatabase', return_value=self.database), mock.patch.object(web, 'SharedCatalog', return_value=self.shared):
            response = web.app.test_client().delete('/api/orders/wb:124/items/124/mapping')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['data']['order']['sale_ready'])
        self.assertNotEqual(self.resolve(self.order)[0]['state'], 'mapped')
        self.assertTrue(web.load_order_product_mappings('wb:123', self.database))

    def link_identity(self, product, barcode, nm_id):
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog_products (name,barcode,external_source,external_product_id,"
                "payload_hash,normalized_payload_json,created_at,updated_at,first_synced_at,last_synced_at) "
                "VALUES ('WB',?,'wildberries',?,'hash','{}','now','now','now','now')",
                (barcode, str(nm_id)),
            )
            connection.execute(
                "UPDATE catalog_excel_products SET bitrix_catalog_product_id=last_insert_rowid() WHERE id=?",
                (product['id'],),
            )

    def test_conflicting_article_and_barcode_block_posting(self):
        other = self.catalog.create_product('Другие часы', article='OTHER', brand='Brand', category='Часы', stock=2)
        self.link_identity(other, '4601', 88)
        with self.assertRaisesRegex(SalesInventoryError, 'Не удалось определить товар ERP'):
            self.service.conduct(self.order)
        self.assertEqual(self.stock(), 3)
        self.assertEqual(self.stock(other), 2)

    def test_second_barcode_or_nm_id_resolves_exact_product(self):
        self.link_identity(self.watch, 'second-barcode', 77)
        self.order['products'][0]['article'] = ''
        self.order['products'][0]['nm_id'] = ''
        self.order['products'][0]['skus'] = ['unknown', 'second-barcode']
        mapped = self.resolve(self.order)[0]
        self.assertEqual(str(mapped['product']['id']), str(self.watch['id']))
        self.order['products'][0]['skus'] = []
        self.order['products'][0]['nm_id'] = 77
        self.assertEqual(self.resolve(self.order)[0]['mapping_method'], 'nm_id')

    def test_manual_mapping_wins_over_conflicting_external_identifiers(self):
        web.save_order_product_mapping('wb:123', '123', self.watch['id'], self.database)
        self.order['products'][0]['article'] = 'unknown'
        with mock.patch.object(web, 'SharedCatalog', return_value=self.shared), mock.patch.object(
            web, 'load_order_product_mappings', return_value=web.load_order_product_mappings('wb:123', self.database)
        ):
            service = WildberriesSales(self.inventory, web.resolve_wildberries_sale_products)
            sale = service.conduct(self.order)
        self.assertEqual(str(sale['product_id']), str(self.watch['id']))

    def test_endpoint_rejects_disallowed_role(self):
        with self.route_context(), mock.patch.object(web, 'auth_is_enabled', return_value=True), mock.patch.object(web, 'current_auth_user', return_value={'role': 'viewer'}):
            response = web.app.test_client().post('/order/wildberries/123/conduct-sale')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.stock(), 3)

    def test_missing_order_returns_business_error(self):
        with self.route_context(), mock.patch.object(web, 'OrdersSnapshotStore', return_value=mock.Mock(get=mock.Mock(return_value=None))):
            response = web.app.test_client().post('/order/wildberries/123/conduct-sale', headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('не найден', response.json['message'])

    def test_card_and_assembly_read_database_posting_status(self):
        sale = self.service.conduct(self.order)
        store = mock.Mock(query=mock.Mock(return_value={'rows': [self.order]}))
        with mock.patch.object(web, 'load_order_product_mappings', return_value={}), web.app.test_request_context('/sales?view=assembly'):
            rows = web.build_wb_fbs_assembly_rows(store, self.shared)
        self.assertEqual(rows[0]['sale_id'], sale['id'])
        self.assertEqual(rows[0]['status'], self.order['status_name'])

    def test_card_renders_wb_form_then_persisted_sale_link(self):
        with self.route_context(), mock.patch.object(web, 'get_orders', return_value=[self.order]), mock.patch.object(web, 'SharedCatalog', return_value=self.shared), mock.patch.object(web, 'load_order_product_mappings', return_value={}), mock.patch.object(web, 'load_stock_operations', return_value=[]):
            client = web.app.test_client()
            page = client.get('/order/wildberries/123')
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn('/order/wildberries/123/conduct-sale', html)
            self.assertIn('data-wb-sale-form', html)
            self.assertNotIn('data-open-strap-replacement>', html)
            self.assertNotIn('id="orderSaleCommission', html)
            self.assertNotIn('/order/wb:123/stock-writeoff', html)
            # Parse rendered inline JavaScript without launching a browser.
            import re
            import shutil
            import subprocess
            node = shutil.which('node')
            if node:
                for index, script in enumerate(re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)):
                    path = Path(self.temp.name) / ('inline-%d.js' % index)
                    path.write_text(script)
                    check = subprocess.run([node, '--check', str(path)], capture_output=True, text=True)
                    self.assertEqual(check.returncode, 0, check.stderr)
                location_function = re.search(
                    r"    function initializeOrderSaleLocations\(\)\{.*?\n    \}", html, re.S
                ).group(0)
                path = Path(self.temp.name) / 'wb-location.js'
                path.write_text(
                    'const document={getElementById:()=>null}; const window={};\n'
                    + location_function + '\ninitializeOrderSaleLocations();'
                )
                check = subprocess.run([node, str(path)], capture_output=True, text=True)
                self.assertEqual(check.returncode, 0, check.stderr)
            sale = self.service.conduct(self.order)
            posted = client.get('/order/wildberries/123').get_data(as_text=True)
            self.assertIn('Продажа проведена', posted)
            self.assertIn('source=wildberries&sale_id=' + sale['id'], posted)
            self.assertNotIn(' data-wb-sale-form data-order-sale-form', posted)
