"""Real selector events and theme/layout regression; no app, DB or integrations."""
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
STYLES = ('multiwarehouse', 'warehouse', 'erp-components', 'products-workspace', 'themes')
CHECK = r"""
window.addEventListener('error', event => {
    document.body.dataset.selectorError = event.message;
});
window.addEventListener('load', () => {
    try {
        const assert = (ok, message) => { if (!ok) throw Error(message); };
        const url = new window.URL(window.location.href);
        const steps = ['default', 'hong-kong', 'default', 'all'];
        const step = Number(sessionStorage.getItem('selectorStep') || 0);
        const select = document.getElementById('warehouseSelector');
        assert(select.value === steps[step], 'selected warehouse after navigation');
        assert((url.searchParams.get('warehouse_id') || 'default') === steps[step], 'warehouse URL');
        assert(url.searchParams.get('q') === 'eclipse', 'search lost');
        assert(url.searchParams.get('brand') === 'Ziiiro', 'brand lost');
        assert(url.searchParams.get('sort_by') === 'stock', 'sort lost');
        assert(url.searchParams.get('per_page') === '100', 'page size lost');
        if (step) assert(!url.searchParams.has('page'), 'page not reset');
        const mirror = select.parentElement.querySelector('.products-warehouse-select__value');
        assert(mirror.textContent === select.selectedOptions[0].textContent, 'selected width caption stale');
        const selectedStyle = getComputedStyle(select);
        const measure = document.createElement('canvas').getContext('2d');
        measure.font = selectedStyle.font;
        const selectedWidth = measure.measureText(select.selectedOptions[0].textContent).width;
        const actualWidth = select.getBoundingClientRect().width;
        assert(actualWidth >= selectedWidth + 26 && actualWidth <= selectedWidth + 40,
            'select must fit the current option, not the longest one');
        if (step < steps.length - 1) {
            sessionStorage.setItem('selectorStep', String(step + 1));
            select.value = steps[step + 1];
            select.dispatchEvent(new Event('change', {bubbles:true}));
            return;
        }
        select.focus();
        const wrapper = select.closest('.products-warehouse-select');
        assert(wrapper, 'dedicated wrapper missing');
        const label = wrapper.querySelector('.products-warehouse-select__label');
        const box = wrapper.getBoundingClientRect();
        const field = select.getBoundingClientRect();
        const caption = label.getBoundingClientRect();
        const style = getComputedStyle(select);
        assert(caption.right + 4 <= field.left, 'label overlaps select');
        assert(field.left >= box.left && field.right <= box.right, 'select outside frame');
        assert(field.top >= box.top && field.bottom <= box.bottom, 'select taller than frame');
        assert(box.left >= 0 && box.right <= innerWidth, 'control outside viewport');
        assert(box.right <= document.querySelector('main').getBoundingClientRect().right,
            'control outside narrow layout');
        assert(style.borderTopWidth === '0px', 'nested border');
        assert(style.outlineStyle === 'none', 'nested focus ring');
        assert(getComputedStyle(wrapper).outlineStyle === 'solid', 'keyboard focus missing');
        const button = document.getElementById('productsActionsToggle').getBoundingClientRect();
        assert(Math.abs(button.height - box.height) <= 1, 'height differs from adjacent button');
        const canvas = document.createElement('canvas').getContext('2d');
        canvas.font = style.font;
        assert(canvas.measureText(select.selectedOptions[0].textContent).width + 26 <= field.width,
            'long option text clipped');
        document.getElementById('productsActionsMenu').hidden = false;
        const exportItem = document.getElementById('openProductExport');
        const transferItem = document.querySelector('#productsActionsMenu a[role="menuitem"]');
        const exportStyle = getComputedStyle(exportItem);
        const transferStyle = getComputedStyle(transferItem);
        for (const property of ['paddingLeft', 'paddingRight', 'fontSize', 'fontWeight', 'lineHeight']) {
            assert(exportStyle[property] === transferStyle[property], 'menu item differs: ' + property);
        }
        assert(transferStyle.textDecorationLine === 'none', 'transfer link underlined');
        assert(Math.abs(exportItem.getBoundingClientRect().height - transferItem.getBoundingClientRect().height) <= 1,
            'menu item heights differ');
        document.body.dataset.selectorCheck = 'pass';
    } catch (error) {
        document.body.dataset.selectorError = error.message;
    }
});
"""


class SelectorFixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        assets = {'/static/css/' + name + '.css': ROOT / ('app/static/css/' + name + '.css')
                  for name in STYLES + ('sync-popover',)}
        assets['/static/js/multiwarehouse.js'] = ROOT / 'app/static/js/multiwarehouse.js'
        if url.path in assets:
            content = assets[url.path].read_bytes()
            mime = 'text/css' if url.path.endswith('.css') else 'text/javascript'
        elif url.path == '/app/products':
            parameters = parse_qs(url.query)
            warehouse = parameters.get('warehouse_id', ['default'])[0]
            theme = parameters.get('theme', ['classic'])[0]
            theme = theme if theme in ('classic', 'dark') else 'classic'
            width = parameters.get('fixture_width', [''])[0]
            # Windows headless Chrome has a minimum window size; constrain the
            # fixture as well so 320/390px layout is still actually exercised.
            width = width + 'px' if width in ('320', '390', '1440') else '100%'
            environment = Environment(loader=FileSystemLoader(str(ROOT / 'app/templates')), autoescape=True)
            environment.globals['url_for'] = lambda *args, **kwargs: '/app/products'
            macro = environment.get_template('_products_workspace.html').module.products_workspace_header
            header = macro('products', {'positions': 0, 'in_stock': 0, 'out_of_stock': 0, 'units': 0},
                           add_button=True, warehouses=[{'id': 'default', 'name': 'Основной TTT'},
                                                       {'id': 'hong-kong', 'name': 'Гонконг'}],
                           selected_warehouse=warehouse)
            links = ''.join('<link rel="stylesheet" href="/static/css/{}.css">'.format(name)
                            for name in STYLES)
            content = ('<!doctype html><html lang="ru" data-theme="{}"><head>'
                       '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                       '{}<script defer src="/static/js/multiwarehouse.js"></script></head>'
                       '<body class="warehouse-page"><main style="padding:16px;width:{};max-width:100%">{}'
                       '</main><script>{}</script></body></html>').format(theme, links, width, header, CHECK).encode('utf-8')
            mime = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'none'; connect-src 'none'")
        self.end_headers()
        self.wfile.write(content)


class WarehouseSelectorBrowserTest(unittest.TestCase):
    def test_switch_keeps_filters_and_single_focus_frame(self):
        candidates = [os.environ.get('CHROME_BIN'), shutil.which('google-chrome'),
                      shutil.which('chromium'), shutil.which('chromium-browser'),
                      r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
        chrome = next((path for path in candidates if path and Path(path).is_file()), None)
        if not chrome:
            self.skipTest('Chrome/Chromium is unavailable')
        server = ThreadingHTTPServer(('127.0.0.1', 0), SelectorFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for width in (1440, 390, 320):
                for theme in ('classic', 'dark'):
                    with self.subTest(width=width, theme=theme), tempfile.TemporaryDirectory() as profile:
                        url = ('http://127.0.0.1:{}/app/products?q=eclipse&brand=Ziiiro'
                               '&sort_by=stock&per_page=100&page=3&theme={}&fixture_width={}').format(server.server_port, theme, width)
                        result = subprocess.run([
                            chrome, '--headless=new', '--no-sandbox', '--disable-gpu',
                            '--disable-background-networking', '--disable-component-update',
                            '--disable-sync', '--no-first-run', '--disable-dev-shm-usage',
                            '--user-data-dir=' + profile, '--window-size={},900'.format(width),
                            '--virtual-time-budget=2000', '--dump-dom', url,
                        ], capture_output=True, encoding='utf-8', timeout=35)
                        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                        self.assertIn('data-selector-check="pass"', result.stdout, result.stdout[-6000:])
                        self.assertNotIn('data-selector-error=', result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
