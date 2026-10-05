"""Local toolbar contracts: synthetic Jinja/DOM only, no browser or integrations."""
import html
import re
import subprocess
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


class WarehouseToolbarContractTest(unittest.TestCase):
    def render(self, selected):
        environment = Environment(loader=FileSystemLoader(str(ROOT / 'app/templates')), autoescape=True)
        environment.globals['url_for'] = lambda *args, **kwargs: '/app/products'
        return str(environment.get_template('_products_workspace.html').module.products_workspace_header(
            'products', {'positions': 0, 'in_stock': 0, 'out_of_stock': 0, 'units': 0},
            add_button=True, selected_warehouse=selected,
            warehouses=[{'id': 'default', 'name': 'Основной TTT'},
                        {'id': 'hong-kong', 'name': 'Гонконг'},
                        {'id': 'custom', 'name': 'Склад <2> & длинное название'}],
        ))

    def test_only_selected_caption_controls_width(self):
        for selected, caption in [('default', 'Основной TTT'), ('hong-kong', 'Гонконг'),
                                  ('all', 'Все склады (с учётом пути)'),
                                  ('custom', 'Склад <2> & длинное название')]:
            with self.subTest(selected=selected):
                markup = self.render(selected)
                value = re.search(r'<span class="products-warehouse-select__value" aria-hidden="true">(.*?)</span>', markup).group(1)
                self.assertEqual(html.unescape(value), caption)
                self.assertIn('id="warehouseSelector" aria-label="Склад"', markup)
                self.assertEqual(len(re.findall('<option ', markup)), 4)
                if selected == 'custom':
                    self.assertNotIn('<2>', value)
        css = (ROOT / 'app/static/css/multiwarehouse.css').read_text(encoding='utf-8')
        selector = re.search(r'\.products-warehouse-select #warehouseSelector\s*\{([^}]+)', css).group(1)
        self.assertIn('position:absolute', selector)
        self.assertIn('width:100%', selector)
        self.assertNotIn('flex:1', selector)
        self.assertIn('visibility:hidden;white-space:pre', css)

    def test_transfer_link_uses_same_menuitem_styles_as_export(self):
        markup = self.render('default')
        self.assertIn('<a href="/warehouse/transfers" role="menuitem">Перемещения</a>', markup)
        css = (ROOT / 'app/static/css/warehouse.css').read_text(encoding='utf-8')
        rule = re.search(r'\.products-add-menu-list > \[role="menuitem"\]\s*\{([^}]+)', css).group(1)
        for declaration in ('display:block', 'width:100%', 'padding:9px 10px', 'font:inherit', 'text-decoration:none'):
            self.assertIn(declaration, rule)
        self.assertIn('[role="menuitem"]:focus-visible', css)
        self.assertIn('[role="menuitem"]:disabled', css)
        self.assertIn('role="menuitem" disabled', self.render('hong-kong'))

    def test_change_and_back_navigation_refresh_caption_without_losing_filters(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = {}, windowListeners = {};
const mirror = {textContent: ''};
const selector = {
    value: 'default', selectedOptions: [{textContent: 'Основной TTT'}],
    parentElement: {querySelector: () => mirror},
    addEventListener: (name, listener) => listeners[name] = listener,
};
let destination;
const location = {
    href: 'https://erp.test/app/products?q=eclipse&brand=Ziiiro&page=3',
    assign: value => { destination = value; },
};
const context = {
    URL, location,
    window: {URL, location, addEventListener: (name, listener) => windowListeners[name] = listener},
    document: {getElementById: () => selector, querySelectorAll: () => [], addEventListener: () => {}},
};
const source = fs.readFileSync(process.argv[1], 'utf8');
vm.runInNewContext(source, context);
assert.equal(mirror.textContent, 'Основной TTT');
selector.value = 'hong-kong';
selector.selectedOptions = [{textContent: 'Гонконг'}];
listeners.change({currentTarget: selector});
assert.equal(mirror.textContent, 'Гонконг');
const target = new URL(destination);
assert.equal(target.searchParams.get('warehouse_id'), 'hong-kong');
assert.equal(target.searchParams.get('q'), 'eclipse');
assert.equal(target.searchParams.get('brand'), 'Ziiiro');
assert.equal(target.searchParams.has('page'), false);
selector.value = 'default';
selector.selectedOptions = [{textContent: 'Основной TTT'}];
windowListeners.pageshow();
assert.equal(mirror.textContent, 'Основной TTT');
assert.equal(selector.title, 'Основной TTT');
context.document.getElementById = () => null;
vm.runInNewContext(source, context); // Other catalog tabs need no warehouse selector.
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'app/static/js/multiwarehouse.js')],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
