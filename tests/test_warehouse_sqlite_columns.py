"""Stable report column names, plus synthetic SELECT replay for old SQLite.

WAREHOUSE_SQL_REPLAY=1 emits compressed synthetic tables/queries, never real data.
Replay needs only sqlite3 and an in-memory database, not the ERP application.
"""
import base64
import json
import os
import unittest
import zlib
from unittest.mock import patch

import test_warehouse_reporting_scope as fixture
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.warehouse_stock_scope import WarehouseStockScope


class WarehouseSqliteColumnsTest(unittest.TestCase):
    setUp = fixture.WarehouseReportingScopeTest.setUp
    product = fixture.WarehouseReportingScopeTest.product

    def test_report_column_names_are_explicit_across_scopes(self):
        uncategorized = self.product('Uncategorized', hong_kong=5, ttt=2)
        with self.db.transaction() as connection:
            connection.execute('UPDATE catalog_excel_products SET model=? WHERE id=?', ('Model A', self.pid))
            connection.execute('UPDATE catalog_excel_products SET category_id=NULL,model=? WHERE id=?',
                               ('Model B', uncategorized))
        queries = []
        original = WarehouseStockScope.execute

        def checked(scope, connection, sql, parameters=()):
            short = connection.execute('PRAGMA short_column_names').fetchone()[0]
            full = connection.execute('PRAGMA full_column_names').fetchone()[0]
            try:
                # Do not let modern SQLite supply convenient implicit names.
                connection.execute('PRAGMA short_column_names=OFF')
                connection.execute('PRAGMA full_column_names=OFF')
                cursor = original(scope, connection, sql, parameters)
                keys = [column[0] for column in cursor.description]
                self.assertFalse(any('.' in key for key in keys), keys)
                rows = [list(row) for row in cursor.fetchall()]
                queries.append({'sql': sql.replace('reporting_products', scope.products),
                                'parameters': list(parameters), 'keys': keys, 'rows': rows})
            finally:
                connection.execute('PRAGMA short_column_names={}'.format(short))
                connection.execute('PRAGMA full_column_names={}'.format(full))
            return original(scope, connection, sql, parameters)

        excel = ExcelProductCatalog(self.db)
        with patch.object(WarehouseStockScope, 'execute', checked):
            for warehouse in ('all', 'default', 'hong-kong'):
                self.catalog.list_brand_overviews(warehouse_id=warehouse)
                self.catalog.list_brand_summaries(warehouse_id=warehouse)
                for sort in ('name', 'stock', 'in_stock'):
                    result = self.catalog.list_category_overviews(
                        warehouse_id=warehouse, sort_by=sort, include_brands=True)
                    self.assertTrue(result['items'])
                excel.stock_tab_counts(warehouse_id=warehouse)
                excel.product_analytics(warehouse_id=warehouse)
                for category in (0, self.watch_category):
                    excel.stock_analytics(category, warehouse_id=warehouse)
                for kind in ('brand', 'category', 'model'):
                    self.catalog.warehouse_filter_options(
                        kind, warehouse, brand_id=self.brand_id, category_id=self.watch_category)

        if os.environ.get('WAREHOUSE_SQL_REPLAY') == '1':
            tables = {}
            with self.db.connect() as connection:
                for name in ('catalog_excel_products', 'catalog_excel_batches',
                             'erp_brands', 'erp_categories', 'erp_brand_categories',
                             'erp_models', 'erp_warehouses', 'erp_warehouse_stocks',
                             'erp_stock_transfers', 'erp_stock_transfer_items',
                             'erp_component_inventory'):
                    columns = connection.execute('PRAGMA table_info(' + name + ')').fetchall()
                    tables[name] = {
                        'columns': [[row['name'], row['type']] for row in columns],
                        'rows': [list(row) for row in connection.execute('SELECT * FROM ' + name)]}
            payload = json.dumps({'tables': tables, 'queries': queries}).encode('utf-8')
            print('SQL_REPLAY=' + base64.b64encode(zlib.compress(payload)).decode('ascii'))


if __name__ == '__main__':
    unittest.main()
