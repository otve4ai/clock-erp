"""Sales show the document warehouse, never a guess from the sales channel."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import template_rendered

from app import web
from app.catalog_db import CatalogDatabase


ROOT = Path(__file__).resolve().parents[1]


class SalesWarehouseColumnTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = CatalogDatabase(Path(temporary.name) / 'catalog.db')
        self.db.initialize()
        env = patch.dict('os.environ', {'CATALOG_DATABASE_PATH': str(self.db.path)})
        env.start()
        self.addCleanup(env.stop)

    def sale(self, identity, source='Amazon', **extra):
        return dict(id=identity, product_id='synthetic-product', product_name='Часы',
                    source=source, quantity=1, unit_price=100,
                    created_at='2026-10-08T12:00:00+03:00', **extra)

    def records(self, sales=(), operations=()):
        return web.build_sales_report_records(
            warehouse_items=[], operations=list(operations),
            stored_manual_sales=list(sales), automatic_overrides={})

    def test_manual_sales_use_document_not_channel_and_legacy_stays_ttt(self):
        records = self.records([
            self.sale('amazon-hk', warehouse_id='hong-kong'),
            self.sale('amazon-ttt', warehouse_id='default'),
            self.sale('wb-hk', source='Wildberries', warehouse_id='hong-kong'),
            self.sale('legacy-amazon'),
        ])
        names = {sale['id']: sale['warehouse_name'] for sale in records}
        self.assertEqual(names, {
            'amazon-hk': 'Гонконг', 'amazon-ttt': 'Основной TTT',
            'wb-hk': 'Гонконг', 'legacy-amazon': 'Основной TTT',
        })
        for sale in records:
            self.assertEqual(web.get_sales_export_value(sale, 'warehouse_name'), names[sale['id']])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM erp_sales').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM erp_warehouse_stocks').fetchone()[0], 0)

    def test_automatic_sales_keep_operation_warehouse(self):
        base = dict(source='Заказ Битрикс', type='writeoff', quantity=1,
                    product_id='synthetic-product', created_at='2026-10-08',
                    sales_source='Amazon')
        records = self.records(operations=[dict(base, id='auto-hk', warehouse_id='hong-kong'),
                                          dict(base, id='auto-legacy')])
        self.assertEqual({sale['id']: sale['warehouse_name'] for sale in records},
                         {'auto-hk': 'Гонконг', 'auto-legacy': 'Основной TTT'})

    def test_inactive_renamed_and_unknown_warehouses_are_not_replaced_by_ttt(self):
        with self.db.transaction() as connection:
            connection.execute("UPDATE erp_warehouses SET name=?, active=0 WHERE id='hong-kong'",
                               ('Гонконг — прежний склад',))
        records = self.records([
            self.sale('old', warehouse_id='hong-kong', status='cancelled'),
            self.sale('unknown', warehouse_id='unknown-id'),
        ])
        self.assertEqual({sale['id']: sale['warehouse_name'] for sale in records}, {
            'old': 'Гонконг — прежний склад',
            'unknown': 'Неизвестный склад (unknown-id)',
        })

    def test_all_tabs_render_warehouse_on_desktop_mobile_and_support_sort_search(self):
        records = self.records([
            self.sale('amazon-hk', warehouse_id='hong-kong'),
            self.sale('amazon-ttt', warehouse_id='default'),
            self.sale('ttt', source='Tictactoy', warehouse_id='default'),
            self.sale('wb', source='Wildberries', warehouse_id='default'),
        ])
        contexts = []

        def rendered(sender, template, context, **extra):
            contexts.append(context)

        with patch.dict(web.app.config, TESTING=True, AUTH_TESTING=False), patch.object(
            web, 'api_sales_records', return_value=records
        ), patch.object(web.CDEK_SALES, 'rows', return_value=[]), template_rendered.connected_to(
            rendered, web.app
        ):
            client = web.app.test_client()
            for source in ('all', 'amazon', 'tictactoy', 'wildberries'):
                with self.subTest(source=source):
                    response = client.get('/app/sales', query_string={
                        'source': source, 'sort': 'warehouse_name', 'sort_dir': 'asc'})
                    self.assertEqual(response.status_code, 200)
                    body = response.get_data(as_text=True)
                    self.assertIn('data-column-key="warehouse_name"', body)
                    self.assertIn('sales-mobile-warehouse', body)
                    self.assertIn({'key': 'warehouse_name', 'label': 'Склад'}, contexts[-1]['sales_columns'])
                    self.assertRegex(body, r'Склад:\s*(Гонконг|Основной TTT)')
                    names = [sale['warehouse_name'] for sale in contexts[-1]['sales']]
                    self.assertEqual(names, sorted(names))
            response = client.get('/app/sales', query_string={'source': 'all', 'q': 'Гонконг'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([sale['id'] for sale in contexts[-1]['sales']], ['amazon-hk'])


class SalesWarehouseLayoutTest(unittest.TestCase):
    def test_first_paint_and_settings_preserve_user_layout_when_adding_warehouse(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is unavailable')
        template = (ROOT / 'app/templates/sales.html').read_text(encoding='utf-8')
        sanitize = re.search(r'        function sanitizeOrder\(value\) \{.*?\n        \}',
                             template, re.S).group(0)
        recommended = re.search(r'const recommendedOrder = (\[.*?\]);', template, re.S).group(1)
        program = r'''
const fs = require('fs');
const vm = require('vm');
const options = JSON.parse(process.argv[2]);
const context = {
    localStorage: {getItem: () => JSON.stringify(options.saved)},
    ErpTableLayout: require(process.argv[3]),
    document: {getElementById: () => null},
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const table = {
    dataset: {salesSettingsKey: 'test'}, style: {},
    closest: (selector) => selector === '.table-wrap' ? {clientWidth: 1600} : null,
    querySelectorAll: () => [], querySelector: () => null,
};
const view = context.SalesTableInitialLayout.apply(table, options.columns.map(key => ({key})));
context.availableColumns = options.columns;
context.savedOrder = options.saved.order;
const normalized = vm.runInNewContext(
    `const recommendedOrder = ${options.recommended};
     const defaultOrder = recommendedOrder.filter(key => availableColumns.includes(key));
     const pinnedColumns = ['created_at', 'order_number'];
     ${options.sanitize}; sanitizeOrder(savedOrder);`, context);
process.stdout.write(JSON.stringify({view, normalized}));
'''
        base = ['created_at', 'order_number', 'source', 'product_name', 'article', 'note']
        for channel_only in (False, True):
            old = [key for key in base if not (channel_only and key == 'source')]
            columns = old + ['warehouse_name']
            for customized in (False, True):
                saved_order = old + (['warehouse_name'] if customized else [])
                saved = dict(version=6, order=saved_order, hidden=['note'],
                             widths={'product_name': 411}, customWidths=['product_name'])
                if customized:
                    saved['hidden'].append('warehouse_name')
                    saved['widths']['warehouse_name'] = 175
                with self.subTest(channel_only=channel_only, customized=customized):
                    result = subprocess.run([
                        node, '-e', program, str(ROOT / 'app/static/js/sales-table-initial-layout.js'),
                        json.dumps(dict(columns=columns, saved=saved, sanitize=sanitize, recommended=recommended)),
                        str(ROOT / 'app/static/js/erp-table-layout.js'),
                    ], check=True, capture_output=True, text=True)
                    data = json.loads(result.stdout)
                    view = data['view']
                    self.assertEqual(view['order'], data['normalized'])
                    expected = list(saved_order)
                    if not customized:
                        anchor = 'order_number' if channel_only else 'source'
                        expected.insert(expected.index(anchor) + 1, 'warehouse_name')
                    self.assertEqual(view['order'], expected)
                    self.assertEqual(view['hidden'], saved['hidden'])
                    self.assertEqual(view['widths']['product_name'], 411)
                    self.assertEqual(view['widths']['warehouse_name'], 175 if customized else 132)


if __name__ == '__main__':
    unittest.main()
