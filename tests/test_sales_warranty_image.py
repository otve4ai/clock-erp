import unittest
from pathlib import Path

from app import web


class SalesWarrantyImageTest(unittest.TestCase):
    def test_order_21147_warranty_position_is_detected_strictly(self):
        order_21147_position = 'Гарантия на товар «Bradley Element»'

        self.assertTrue(
            web.is_warranty_sale_product(order_21147_position)
        )
        self.assertFalse(web.is_warranty_sale_product('Bradley Element'))
        self.assertFalse(
            web.is_warranty_sale_product(
                'Расширенная гарантия Bradley Element'
            )
        )

    def test_warranty_uses_badge_while_regular_photo_flow_is_preserved(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'app/templates/sales.html'
        ).read_text(encoding='utf-8')
        macro_source = source.split(
            '{% macro render_sales_product_thumb', 1
        )[1].split('{%- endmacro %}', 1)[0]
        macro_source = (
            '{% macro render_sales_product_thumb'
            + macro_source
            + '{%- endmacro %}'
        )

        def render_thumb(image_url, is_warranty):
            template = web.app.jinja_env.from_string(
                macro_source
                + '{{ render_sales_product_thumb(image_url, is_warranty) }}'
            )
            return template.render(
                image_url=image_url,
                is_warranty=is_warranty,
            )

        warranty_html = render_thumb('', True)
        local_photo_html = render_thumb('/product-images/42', False)
        missing_photo_html = render_thumb('', False)

        self.assertIn('sales-product-thumb is-warranty', warranty_html)
        self.assertIn('sales-warranty-badge', warranty_html)
        self.assertNotIn('sales-product-placeholder', warranty_html)
        self.assertIn('src="/product-images/42"', local_photo_html)
        self.assertIn('class="sales-product-thumb"', local_photo_html)
        self.assertNotIn(
            'class="sales-product-thumb is-placeholder"',
            local_photo_html,
        )
        self.assertIn('is-placeholder', missing_photo_html)
        self.assertIn('sales-product-placeholder', missing_photo_html)


if __name__ == '__main__':
    unittest.main()
