import unittest
from pathlib import Path

from app.services.bitrix_site_status import bitrix_site_status


ROOT = Path(__file__).resolve().parents[1]


class WarehouseSiteStatusTest(unittest.TestCase):
    def test_projection_distinguishes_all_safe_site_states(self):
        expected = (
            ("100", 1, "active", "Активен"),
            ("100", 0, "inactive", "Выключен"),
            (None, None, "unlinked", "Нет связи"),
            ("100", None, "unknown", "Неизвестно"),
            ("100", "", "unknown", "Неизвестно"),
        )
        for external_id, active, key, label in expected:
            with self.subTest(key=key, active=active):
                status = bitrix_site_status(external_id, active)
                self.assertEqual((status["key"], status["label"]), (key, label))

    def test_products_table_contains_read_only_site_status_column(self):
        template = (ROOT / "app/templates/warehouse.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('data-column-key="site_status">На сайте</th>', template)
        self.assertIn("{{ item.site_status_label }}", template)
        self.assertNotIn('type="checkbox" data-site-status', template)


if __name__ == "__main__":
    unittest.main()
