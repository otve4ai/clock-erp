import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductsCompactToolbarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "app/templates/warehouse.html").read_text(
            encoding="utf-8"
        )
        cls.focus_script = (ROOT / "app/static/js/erp-focus-mode.js").read_text(
            encoding="utf-8"
        )

    def test_retired_collection_controls_are_absent(self):
        self.assertNotIn("Подборки", self.template)
        self.assertNotIn("productCollection", self.template)

    def test_sales_style_table_controls_are_directly_available(self):
        toolbar = self.template.split('id="warehouseSearchForm"', 1)[1].split(
            "</form>", 1
        )[0]
        for control in (
            'id="warehouseColumnSettingsTrigger"',
            'id="warehouseFocusModeToggle"',
        ):
            self.assertIn(control, toolbar)
        for label in (
            "Столбцы",
            "Развернуть",
        ):
            self.assertIn(label, toolbar)
        self.assertNotIn('id="warehouseMoreTrigger"', toolbar)
        self.assertNotIn('id="warehouseMoreMenu"', toolbar)
        self.assertIn('event.key === "Escape" && !panel.hidden', self.template)
        self.assertIn(
            '!event.target.closest(".warehouse-column-settings")',
            self.template,
        )

    def test_columns_and_focus_keep_existing_controllers(self):
        self.assertIn(
            "function initializeWarehouseColumnSettings", self.template
        )
        self.assertIn('id="warehouseTableReset"', self.template)
        self.assertIn("data-erp-focus-mode-toggle", self.template)
        self.assertIn("label.textContent = action + labelSuffix", self.focus_script)
        self.assertNotIn('data-focus-mode-label-suffix=" таблицу"', self.template)



if __name__ == "__main__":
    unittest.main()
