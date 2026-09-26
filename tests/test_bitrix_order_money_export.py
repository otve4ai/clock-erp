"""Run the actual PHP endpoint with fake Bitrix classes and no database/network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from app.clients.bitrix_orders import normalize_order


PHP_STUB = r'''<?php
namespace Bitrix\Main {
    class Application {
        public static function getConnection() { return new \FakeConnection(); }
    }
}
namespace {
    $_GET['id'] = '21147';
    $GLOBALS['fixture'] = json_decode(getenv('ORDER_FIXTURE'), true);
    class FakeResult {
        private $rows;
        function __construct($rows) { $this->rows = $rows; }
        function fetch() { return array_shift($this->rows); }
    }
    class FakeConnection {
        function query($sql) {
            if ($sql !== 'SELECT PRICE, PRICE_DELIVERY, DISCOUNT_VALUE, TAX_VALUE, CURRENCY FROM b_sale_order WHERE ID = 21147') {
                throw new \Exception('Unexpected query');
            }
            return new FakeResult($GLOBALS['fixture']['money'] ? [$GLOBALS['fixture']['money']] : []);
        }
    }
    class CModule { static function IncludeModule($name) { return true; } }
    class CSaleOrder {
        static function GetByID($id) {
            return ['ID' => $id, 'ACCOUNT_NUMBER' => '21147', 'DATE_INSERT' => '',
                    'STATUS_ID' => 'D', 'PRICE' => '41404', 'CURRENCY' => 'RUB',
                    'PAYED' => 'N', 'USER_ID' => null];
        }
    }
    class CSaleBasket {
        static function GetList($sort, $filter, $group, $nav, $select) {
            return new FakeResult($GLOBALS['fixture']['items']);
        }
    }
    class CSaleOrderPropsValue {
        static function GetOrderProps($id) { return new FakeResult([]); }
    }
}
'''


@unittest.skipUnless(shutil.which('php'), 'PHP CLI required for endpoint regression')
class BitrixOrderMoneyExportTest(unittest.TestCase):
    def export(self, total, item_total, delivery='0.0000', discount='0.0000'):
        fixture = {
            'money': {'PRICE': total, 'PRICE_DELIVERY': delivery,
                      'DISCOUNT_VALUE': discount, 'TAX_VALUE': '0.00', 'CURRENCY': 'RUB'},
            'items': [
                {'PRODUCT_ID': '235052', 'NAME': 'Switch Sunflower Black',
                 'QUANTITY': '1.0000', 'PRICE': '40425.0000', 'CURRENCY': 'RUB'},
                {'PRODUCT_ID': '206112', 'NAME': 'Warranty', 'QUANTITY': '1.0000',
                 'PRICE': item_total, 'CURRENCY': 'RUB'},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prolog = root / 'bitrix/modules/main/include/prolog_before.php'
            prolog.parent.mkdir(parents=True)
            prolog.write_text(PHP_STUB)
            endpoint = Path(__file__).resolve().parents[1] / 'bitrix/order.php'
            runner = root / 'run.php'
            runner.write_text('<?php $_SERVER["DOCUMENT_ROOT"] = __DIR__; require ' +
                              json.dumps(str(endpoint)) + ';')
            result = subprocess.run(
                ['php', str(runner)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(os.environ, ORDER_FIXTURE=json.dumps(fixture)),
            )
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode())
            return json.loads(result.stdout)['order']

    def test_currency_display_rounding_never_changes_exported_total(self):
        for total, warranty in (('41404.0000', '979.0000'),
                                ('41404.0100', '979.0100'),
                                ('41404.0200', '979.0200')):
            with self.subTest(total=total):
                order = self.export(total, warranty)
                self.assertEqual(order['price'], total)
                self.assertEqual(order['products'][1]['price'], warranty)
                self.assertEqual(order['delivery_price'], '0.0000')
                self.assertEqual(order['discount'], '0.0000')
                self.assertNotIn('rounding_adjustment', order)
                self.assertTrue(normalize_order(order)['calculation_consistent'])

    def test_unexplained_actual_differences_are_not_forgiven(self):
        for total in ('41404.0000', '41403.0200', '41394.0200', '41414.0200'):
            with self.subTest(total=total):
                order = self.export(total, '979.0200')
                self.assertEqual(order['price'], total)
                self.assertFalse(normalize_order(order)['calculation_consistent'])

    def test_unknown_adjustments_do_not_explain_missing_kopecks(self):
        order = self.export('41404.0000', '979.0200', delivery=None, discount=None)
        self.assertIsNone(order['delivery_price'])
        self.assertIsNone(order['discount'])
        self.assertFalse(normalize_order(order)['calculation_complete'])


if __name__ == '__main__':
    unittest.main()
