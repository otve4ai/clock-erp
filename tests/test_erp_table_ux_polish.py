import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ErpTableUxPolishTest(unittest.TestCase):
    def source(self, relative_path):
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_products_use_one_toolbar_without_empty_table_floor(self):
        source = self.source("app/templates/warehouse.html")
        toolbar = source.split('id="warehouseSearchForm"', 1)[1].split(
            "</form>", 1
        )[0]
        column_settings = source.split(
            "function initializeWarehouseColumnSettings", 1
        )[1].split("function initializeWarehouseTableView", 1)[0]

        self.assertNotIn('id="warehouseMoreTrigger"', toolbar)
        self.assertNotIn('id="warehouseMoreMenu"', toolbar)
        self.assertNotIn('id="warehouseCollectionModeTrigger"', toolbar)
        self.assertIn('id="warehouseColumnSettingsTrigger"', toolbar)
        self.assertIn('id="warehouseColumnSettingsPanel"', toolbar)
        self.assertIn('id="warehouseFocusModeToggle"', toolbar)
        self.assertIn("Столбцы", toolbar)
        self.assertIn(">Развернуть</span>", toolbar)
        self.assertIn('id="warehouseColumnSettingsList"', toolbar)
        self.assertIn('id="warehouseTableReset"', toolbar)
        self.assertIn('list.replaceChildren()', column_settings)
        self.assertIn(
            'resetWarehouseTableView(table, view, columnController)',
            column_settings,
        )
        self.assertNotIn('class="warehouse-table-toolbar"', source)

    def test_stock_filter_stays_inside_products_workspace(self):
        source = self.source("app/templates/warehouse.html")
        stock_header = source.split(
            '<th data-column-key="stock">', 1
        )[1].split("</th>", 1)[0]
        self.assertNotIn("warehouseInStockToggle", stock_header)
        self.assertNotIn("stock-mini-toggle", stock_header)
        self.assertNotIn('id="warehouseFilterInStock"', source)
        self.assertNotIn('name="in_stock"', source)
        self.assertNotIn("Только в наличии", source)
        self.assertIn('name="stock_state"', source)
        self.assertIn('value="out"', source)

    def test_resize_scroll_and_action_column_contracts_remain(self):
        contracts = {
            "warehouse.html": "js/erp-native-table-columns.js",
            "sales.html": "js/erp-native-table-columns.js",
            "receipts.html": "receipt-column-resize-handle",
        }
        for template, handle in contracts.items():
            with self.subTest(template=template):
                source = self.source("app/templates/" + template)
                self.assertIn(handle, source)
                self.assertIn("data-erp-scroll-hint", source)
                self.assertIn('tabindex="0"', source)
                self.assertIn("js/table-scroll-hint.js", source)

        warehouse = self.source("app/templates/warehouse.html")
        self.assertIn('data-system-column="actions"', warehouse)
        for template in ("sales.html", "receipts.html"):
            source = self.source("app/templates/" + template)
            self.assertIn('data-system-column="actions"', source)

        script = self.source("app/static/js/table-scroll-hint.js")
        self.assertIn("scrollWidth - container.clientWidth", script)
        self.assertIn("is-at-scroll-end", script)
        self.assertIn('event.key !== "ArrowLeft"', script)

    def test_table_headers_and_actions_use_shared_quiet_styles(self):
        css = self.source("app/static/css/erp-components.css")
        self.assertIn(
            ".warehouse-products-table.erp-data-table thead th",
            css,
        )
        self.assertIn(".sales-table.erp-data-table thead th", css)
        self.assertIn(".receipts-table.erp-data-table thead th", css)
        self.assertIn("border-radius: 0 !important", css)
        self.assertIn(".erp-scroll-hint.has-horizontal-overflow", css)
        self.assertIn(".receipt-delete-button:hover", css)
        self.assertIn(".sale-row.is-cancelled td", css)

    def test_product_and_sales_key_numeric_columns_are_centered(self):
        css = self.source("app/static/css/erp-components.css")

        for selector in (
            'th[data-column-key="stock"]',
            'td[data-column-key="stock"]',
            'th[data-column-key="price"]',
            'td[data-column-key="price"]',
            "th.col-quantity_display",
            "td.col-quantity_display",
            "th.col-unit_price_display",
            "td.col-unit_price_display",
        ):
            self.assertIn(selector, css)
        self.assertIn("text-align: center", css)
        self.assertIn("justify-content: center", css)

    def test_report_labels_are_short_and_secondary(self):
        sales = self.source("app/templates/sales.html")
        receipts = self.source("app/templates/receipts.html")
        sales_header = sales.split('id="salesReportLink"', 1)[1].split(
            "</a>", 1
        )[0]
        receipt_toolbar = receipts.split(
            'class="toolbar receipt-filter-toolbar', 1
        )[1].split("</div>\n\n        <section", 1)[0]

        self.assertIn("erp-secondary-action", sales_header)
        self.assertIn("data-report-label>Отчёт", sales_header)
        self.assertNotIn("Сформировать отчёт", sales_header)
        self.assertIn("receipt-report-button erp-secondary-action", receipt_toolbar)
        self.assertIn(">\n                Отчёт\n", receipt_toolbar)

    def test_receipt_column_visibility_reuses_width_storage(self):
        source = self.source("app/templates/receipts.html")
        toolbar = source.split(
            'class="toolbar receipt-filter-toolbar', 1
        )[1].split("</div>\n\n        <section", 1)[0]

        self.assertIn('id="receiptColumnSettingsTrigger"', toolbar)
        self.assertIn('id="receiptColumnSettingsPanel"', toolbar)
        self.assertIn("vechasu-receipts-table-view-v1", source)
        self.assertIn("JSON.stringify({widths, hidden})", source)
        self.assertIn('requiredColumns = ["date", "product"]', source)
        self.assertIn('data-system-column="actions"', source)
        self.assertIn('<col data-column-key="purchase-price">', source)
        self.assertIn('"purchase-price": 126', source)
        self.assertIn("normalizeWidths(saved.widths)", source)
        self.assertIn("Number.isFinite(numeric) && numeric > 0", source)
        self.assertIn("panel.replaceChildren()", source)
        self.assertIn("receipt-column-settings-reset", source)
        self.assertIn("saveReceiptTableView();\n        initializeReceiptColumnSettings()", source)

    def test_table_storage_isolated_and_ajax_initializer_is_idempotent(self):
        warehouse = self.source("app/templates/warehouse.html")
        sales = self.source("app/templates/sales.html")
        receipts = self.source("app/templates/receipts.html")

        self.assertIn("vechasu.warehouse.table-view.v3", warehouse)
        self.assertIn("js/erp-table-layout.js", warehouse)
        self.assertIn('data-sales-settings-key="sales_{{ active_source }}"', sales)
        self.assertIn("vechasu-receipts-table-view-v1", receipts)
        self.assertIn("warehouseTableViewController.abort()", warehouse)
        self.assertIn('delete table.dataset.viewReady', warehouse)
        self.assertIn("initializeWarehouseTableView();", warehouse)
        self.assertIn('if (table.dataset.viewReady === "1") return;', warehouse)

    def test_products_reuse_sales_column_interaction_contract(self):
        warehouse = self.source("app/templates/warehouse.html")
        sales = self.source("app/templates/sales.html")
        controller = self.source("app/static/js/erp-native-table-columns.js")
        css = self.source("app/static/css/erp-native-table-columns.css")

        for template in (warehouse, sales):
            self.assertIn("window.ErpNativeTableColumns.create({", template)
            self.assertNotIn('handle.className = "sales-column-resize-handle"', template)
            self.assertNotIn("let dragState = null", template)
        self.assertIn('const HANDLE_CLASS = "sales-column-resize-handle"', controller)
        self.assertIn('const DROP_BEFORE_CLASS = "sales-drop-before"', controller)
        self.assertIn('Math.abs(event.clientX - dragState.startX) < 6', controller)
        self.assertIn('warehouseTableMaximumWidth = 520', warehouse)
        self.assertIn("snapshotVisibleWidths();", controller)
        self.assertIn("view.widths[key] =", controller)
        self.assertIn("keys.map((key) => [key, view.widths[key]])", controller)
        self.assertIn(
            'table.style.setProperty("min-width", tableWidth, exactPriority)',
            controller,
        )
        self.assertIn('data-system-column="actions"', warehouse)
        for template in (warehouse, sales):
            self.assertIn("erp-native-columns-table", template)
            self.assertIn(
                "static_asset_url('css/erp-native-table-columns.css')",
                template,
            )
        self.assertIn(
            ".erp-native-columns-table.erp-data-table thead th",
            css,
        )
        self.assertIn(
            ".erp-native-columns-table.erp-data-table .erp-sort-label",
            css,
        )

    def test_resize_completion_suppresses_sort_and_cleans_pointer_handlers(self):
        controller = self.source("app/static/js/erp-native-table-columns.js")
        self.assertIn("suppressClickUntil = Date.now() + 350", controller)
        self.assertRegex(
            controller,
            re.compile(r'removeEventListener\(\s*"pointerup"'),
        )
        self.assertRegex(
            controller,
            re.compile(r'removeEventListener\(\s*"pointercancel"'),
        )

        receipts = self.source("app/templates/receipts.html")
        self.assertIn("suppressSortUntil = Date.now() + 350", receipts)

    def test_wide_tables_are_contained_by_their_scroll_owners(self):
        css = self.source("app/static/css/erp-components.css")
        self.assertIn(".warehouse-page #warehouseResults", css)
        self.assertIn(".sales-page .sales-data-card", css)
        self.assertIn(".table-card:has(.receipts-table)", css)
        self.assertIn(".sales-page .table-wrap:has(.sales-table)", css)
        self.assertIn("overscroll-behavior-x: contain", css)


if __name__ == "__main__":
    unittest.main()
