"""Disposable, loopback-only multiwarehouse demo. Never loads a live database."""
import argparse
import asyncio  # Import before disabling subprocesses (Windows event loop setup).
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare_environment(root):
    if (ROOT / '.env').exists():
        raise RuntimeError('Run the preview only in an isolated checkout without .env')
    # Clear inherited integration settings without reading or printing secrets.
    for name in list(os.environ):
        if name.startswith(('ERP_', 'BITRIX_', 'MOYSKLAD_', 'WB_', 'AMAZON_',
                            'SMTP_', 'SMSBLISS_', 'CDEK_', 'SERVICE_VAULT_')):
            os.environ.pop(name)
        elif name.lower() in ('http_proxy', 'https_proxy', 'all_proxy'):
            os.environ.pop(name)
    paths = {
        'CATALOG_DATABASE_PATH': 'catalog.db', 'ERP_AUTH_DATABASE': 'auth.db',
        'ORDERS_DATABASE_PATH': 'orders.db', 'ERP_TASKS_DATABASE': 'tasks.db',
        'ERP_TASKS_MODULE_DATABASE': 'tasks-module.db',
        'ERP_PURCHASES_DATABASE': 'purchases.db',
        'CUSTOMERS_DATABASE_PATH': 'customers.db', 'ERP_SMS_DATABASE': 'sms.db',
        'ERP_MAIL_DATABASE': 'mail.db', 'ERP_MAIL_ATTACHMENT_ROOT': 'attachments',
        'ERP_SERVICES_DATABASE': 'services.db', 'ERP_BACKUP_ROOT': 'backups',
        'CDEK_CACHE_DIR': 'cdek', 'CDEK_PAYOUTS_DIR': 'cdek-payouts',
    }
    os.environ.update({name: str(root / path) for name, path in paths.items()})
    os.environ.update({
        'ERP_TEST_MODE': '1', 'ERP_TEST_ROOT': str(root),
        'ERP_SOURCE_ROOT': str(ROOT), 'ERP_TASKS_MODULE_ENABLED': '0',
        'ERP_SECRET_KEY': 'synthetic-local-preview-only',
        'ERP_MAIL_SECRET_KEY': 'a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=',
        'SERVICE_VAULT_KEY': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=',
        'ERP_SESSION_COOKIE_SECURE': '0', 'ERP_TRUSTED_PROXY_COUNT': '0',
        'PYTHON_DOTENV_DISABLED': '1', 'NO_PROXY': '*', 'no_proxy': '*',
        'UPDATE_ORDER_STATUS_TOKEN': '', 'APP_PUBLIC_URL': '',
    })
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    if os.name == 'nt':
        # Single-process demo only; not a replacement for production Unix locks.
        sys.modules['fcntl'] = types.SimpleNamespace(
            LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *args: None)
        if not hasattr(os, 'getuid'):
            os.getuid = lambda: 0


def disable_outbound_connections():
    def denied(*args, **kwargs):
        raise OSError('Outgoing connections and subprocesses disabled in local preview')
    # The server accepts inbound loopback connections; it never needs to connect.
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    subprocess.Popen = denied


def seed_catalog(database):
    from app.services.excel_product_catalog import ExcelProductBatchService
    from app.services.manual_receipts import ManualReceipts
    from app.services.warehouse_transfers import WarehouseTransfers
    names = ['X7000 GLADIATOR GREEN', 'X7000 TERMINATOR STEEL',
             'X7000 TERMINATOR BLACK', 'X7000 GLADIATOR WHITE',
             'X7000 GLADIATOR YELLOW', 'X7000 GLADIATOR RED']
    results = []
    for n in range(1000, 0, -1):
        stock = [0, 0, 1, 0, 3, 1][n - 1] if n <= 6 else n % 12
        results.append({
            'excel_row': n + 1,
            'excel_name': names[n - 1] if n <= 6 else 'Демо-модель {:04d}'.format(n),
            'excel_article': 'DEMO-{:04d}'.format(n),
            'excel_brand': 'BENLYDESIGN' if n <= 15 else 'Демо',
            'article_quality': 'code_like', 'category': 'Наручные часы',
            'stock': float(stock), 'stock_valid': True, 'cell': 'A-{:02d}'.format(n % 20),
            'product_id': None, 'match_status': 'not_found',
            'match_method': 'synthetic-preview', 'confidence': 0, 'alternatives': [],
        })
    ExcelProductBatchService(database).apply(results, 'd' * 64, 'DEMO-not-live.xlsx')
    with database.transaction() as c:
        rows = c.execute('SELECT id,excel_article FROM catalog_excel_products').fetchall()
        identities = {int(r['excel_article'].split('-')[1]): r['id'] for r in rows}
        # Synthetic site flags only; these IDs cannot address real products.
        c.execute("UPDATE catalog_excel_products SET bitrix_external_product_id='demo-' || id, "
                  "bitrix_active=CASE WHEN stock>0 THEN 1 ELSE 0 END")
    receipts = ManualReceipts(database)
    for warehouse, numbers in [('hong-kong', range(1, 16))]:
        doc = receipts.create(warehouse, 'initial_stock', [
            {'product_id': identities[n], 'quantity': 100 + n * 5}
            for n in numbers], comment='Демонстрационные остатки, не реальные данные', actor='Демо')
        receipts.post(doc['id'], actor='Демо')
    transfers = WarehouseTransfers(database)
    for number, product, source, destination, actions in [
        (1, 3, 'hong-kong', 'default', ['send', 'receive']),
        (2, 9, 'default', 'hong-kong', ['send']),
        (3, 5, 'default', 'hong-kong', []),
    ]:
        doc = transfers.create(source, destination,
                               [{'product_id': identities[product], 'quantity': 1}],
                               'demo-transfer-{}'.format(number), actor='Демо',
                               comment='Учебный пример — можно пробовать действия')
        for action in actions:
            transfers.transition(doc['id'], action, actor='Демо')


def create_preview(root, port, product_card=False, sales_form=False):
    prepare_environment(root)
    disable_outbound_connections()
    from app.schema_migrations import apply_migrations
    from app.domain_schema_migrations import apply_domain_migrations
    from app.purchases_migrations import migrate_database as migrate_purchases
    from app.customer_registry_migrations import migrate_database as migrate_customers
    from app.sms_migrations import migrate_database as migrate_sms
    from app.mail_migrations import migrate_database as migrate_mail
    from app.catalog_db import CatalogDatabase
    apply_migrations(root / 'catalog.db', app_commit='synthetic-preview')
    for domain in ('auth', 'orders', 'tasks'):
        apply_domain_migrations(root / (domain + '.db'), domain, 'synthetic-preview')
    for filename, migrate in [('purchases.db', migrate_purchases), ('customers.db', migrate_customers),
                              ('sms.db', migrate_sms), ('mail.db', migrate_mail)]:
        migrate(root / filename)
    seed_catalog(CatalogDatabase(root / 'catalog.db'))
    from app import web
    from flask import jsonify, request, redirect
    app = web.app
    app.instance_path = str(root / 'instance')
    app.config.update(TESTING=True, AUTH_TESTING=False, SESSION_COOKIE_NAME='multiwarehouse_demo',
                      ERP_MAINTENANCE_MARKER=str(root / 'maintenance.json'))
    web.CATALOG_TAXONOMY_PATH = root / 'catalog_taxonomy.json'
    web.WAREHOUSE_CREATED_AT_PATH = root / 'warehouse_created_at.json'
    if sales_form:
        # No legacy sales files or external location catalogs in a visual demo.
        web.api_sales_records = lambda *args, **kwargs: []
        web.get_tictactoy_location_catalog = lambda: {}

    def preview_scope():
        if request.host not in ('127.0.0.1:{}'.format(port), 'localhost:{}'.format(port)):
            return 'Local preview only', 403
        if request.method not in ('GET', 'HEAD'):
            if request.headers.get('Origin') not in (None, 'http://' + request.host):
                return 'Local preview only', 403
            allowed_writes = ('transfers', 'transfer_action')
            if product_card and request.method == 'PATCH':
                allowed_writes += ('api_product_resource',)
            if request.endpoint not in allowed_writes:
                return jsonify(ok=False, message='Эта операция отключена в локальном демо.'), 403
        if request.path == '/':
            return redirect('/app/products')
        if sales_form and request.method in ('GET', 'HEAD') and request.endpoint in (
            'sales_page', 'api_catalog_options', 'api_sales_catalog',
            'api_product_bundle', 'api_product_required_strap',
            'api_sales_locations', 'api_sales_sources',
        ):
            return None
        if product_card and (request.endpoint == 'api_product_resource' or (
            request.method in ('GET', 'HEAD') and request.endpoint in (
                'warehouse_product_detail', 'api_product_warehouse_stocks',
                'api_product_movements', 'api_product_bundle',
                'api_product_required_strap', 'api_catalog_options',
            )
        )):
            return None
        if request.endpoint not in ('warehouse_page', 'transfers_page', 'transfers',
                                    'transfer_action', 'transfer_products', 'static', 'preview_health'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, message='Этот раздел отключён в локальном демо.'), 403
            return ('<meta charset="utf-8"><p>Это локальное демо товаров и перемещений.</p>'
                    '<a href="/app/products">Вернуться к товарам</a>'), 403
    app.before_request_funcs[None].insert(0, preview_scope)

    @app.route('/__preview/health')
    def preview_health():
        return jsonify(mode='synthetic-local-only', phase='hong-kong-pilot', products=1000,
                       warehouses=['default', 'hong-kong'], network='blocked')

    @app.after_request
    def demo_notice(response):
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
        response.headers['Cache-Control'] = 'no-store'
        if response.mimetype == 'text/html' and not response.direct_passthrough:
            banner = ('<aside style="position:fixed;bottom:8px;left:50%;transform:translateX(-50%);'
                      'z-index:99999;background:#fff3cd;color:#664d03;border:1px solid #e7c86a;'
                      'padding:8px 16px;border-radius:8px;font:13px system-ui;white-space:nowrap">'
                      'Локальное демо TTT + Гонконг · Все данные вымышлены · '
                      '<a href="/app/products">Товары</a> · '
                      '<a href="/warehouse/transfers">Перемещения</a></aside>')
            response.set_data(response.get_data(as_text=True).replace('</body>', banner + '</body>'))
        return response
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4197)
    parser.add_argument('--product-card', action='store_true',
                        help='Enable product card reads/edits on disposable demo data only')
    parser.add_argument('--sales-form', action='store_true',
                        help='Enable sales form preview; all sales writes remain blocked')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Use a local unprivileged port (1024..65535)')
    # Each invocation gets a brand-new synthetic database, never an existing path.
    with tempfile.TemporaryDirectory(prefix='erp-multiwarehouse-demo-', ignore_cleanup_errors=True) as directory:
        app = create_preview(Path(directory), args.port, product_card=args.product_card,
                             sales_form=args.sales_form)
        print('Synthetic preview: http://127.0.0.1:{}/app/products'.format(args.port), flush=True)
        app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False,
                load_dotenv=False, threaded=False)


if __name__ == '__main__':
    main()
