import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductSiteStatusIndicatorTest(unittest.TestCase):
    def source(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_indicator_is_in_existing_header_before_actions(self):
        header = self.source("app/templates/_products_workspace.html")
        self.assertIn('_product_site_status_indicator.html', header)
        self.assertLess(
            header.index('_product_site_status_indicator.html'),
            header.index('id="productsActionsToggle"'),
        )

    def test_all_indicator_states_and_exact_mismatch_fields_are_present(self):
        panel = self.source("app/templates/_product_site_status_indicator.html")
        script = self.source("app/static/js/product-site-status-sync.js")
        for label in (
            "Сверка сайта ·", "Всё совпадает", "Проверка…",
            "Ошибка сверки", "Нет данных",
        ):
            self.assertIn(label, panel + script)
        self.assertIn("inStockInactive + outOfStockActive", script)
        self.assertNotIn("in_stock_unlinked +", script)
        self.assertNotIn("unknown_statuses +", script)

    def test_panel_is_overlay_and_has_required_close_handlers(self):
        css = self.source("app/static/css/sync-popover.css")
        self.assertIn('@import url("./sync-popover.css")', self.source("app/static/css/products-workspace.css"))
        script = self.source("app/static/js/product-site-status-sync.js")
        self.assertIn(".product-site-sync-panel {", css)
        self.assertIn("position: absolute", css)
        self.assertIn("position: fixed", css)
        self.assertIn('if (event.key === "Escape"', script)
        self.assertIn("!root.contains(event.target)", script)
        self.assertNotIn("runSync();\n        } else", script)

    def test_panel_keeps_existing_text_and_actions(self):
        panel = self.source("app/templates/_product_site_status_indicator.html")
        for text in (
            "Сверка статусов сайта", "TicTacToy", "Обновить",
            "с остатком выключены", "без остатка активны",
            "Автоматически ежедневно в 03:00", "…",
        ):
            self.assertIn(text, panel)

    def test_systemd_units_are_compatible_with_production_systemd_219(self):
        service = self.source(
            "deploy/systemd/vechasu-bitrix-site-status-sync.service"
        )
        timer = self.source(
            "deploy/systemd/vechasu-bitrix-site-status-sync.timer"
        )
        self.assertIn(
            "ExecStartPre=/usr/bin/test ! -e /run/clock-erp-maintenance.json",
            service,
        )
        self.assertNotIn("ExecCondition=", service)
        self.assertIn("OnCalendar=*-*-* 03:00:00", timer)
        self.assertNotIn("Europe/Moscow", timer)


if __name__ == "__main__":
    unittest.main()
