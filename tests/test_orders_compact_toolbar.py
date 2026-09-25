"""Rendered toolbar preserves every status, count and sync action."""
import unittest
from html.parser import HTMLParser
from pathlib import Path

from flask import Flask, render_template


ROOT = Path(__file__).resolve().parents[1]


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class OrdersCompactToolbarTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(ROOT / 'app/templates'))
        self.counts = {'A': 25, 'C': 1258, 'D': 7504, 'N': 728,
                       **{'WB_' + str(i): i for i in range(8)}}

    def render_filters(self, status='all'):
        with self.app.test_request_context('/app/orders?status=' + status):
            return render_template('_orders_filters.html',
                                   order_source_counts={'all': 16564},
                                   order_status_counts=self.counts,
                                   order_status_label=lambda key: 'Название ' + key)

    def test_every_status_and_count_is_rendered_once_in_dropdown(self):
        html = self.render_filters()
        elements = Elements(html).elements
        buttons = [attrs['data-status-filter'] for _, attrs in elements
                   if 'data-status-filter' in attrs]
        self.assertEqual(buttons, ['all', *self.counts])
        for key, count in self.counts.items():
            self.assertIn(f'data-order-status-count="{key}">{count}</strong>', html)
        self.assertNotIn('status-filter-tabs', html)
        self.assertLess(html.index('data-source-filter="wildberries"'), html.index('data-status-more'))
        self.assertIn('Статус: Все', html)

    def test_selected_overflow_status_and_zero_count_remain_visible(self):
        for key in ('D', 'WB_0', 'WB_7'):
            html = self.render_filters(key.lower())
            self.assertIn(f'Статус: Название {key} · {self.counts[key]}', html)
            pressed = [attrs['data-status-filter'] for _, attrs in Elements(html).elements
                       if 'data-status-filter' in attrs and attrs['aria-pressed'] == 'true']
            self.assertEqual(pressed, [key])

    def test_no_matches_keeps_requested_status_label(self):
        html = self.render_filters('missing')
        self.assertIn('Статус: Название MISSING · 0', html)

    def test_shared_styles_keep_original_geometry(self):
        css = (ROOT / 'app/static/css/sync-popover.css').read_text(encoding='utf-8')
        self.assertIn('width: min(540px, calc(100vw - 32px))', css)
        self.assertIn('@media (max-width: 767px)', css)
        self.assertIn('.product-site-sync-control.is-open', css)
        self.assertIn('products-workspace-button:focus-visible', css)


if __name__ == '__main__':
    unittest.main()
