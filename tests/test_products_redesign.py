import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductsRedesignStructureTest(unittest.TestCase):
    def source(self, name):
        return (ROOT / "app" / "templates" / name).read_text(encoding="utf-8")

    def test_four_tabs_are_persistent_on_all_catalog_screens(self):
        workspace = self.source("_products_workspace.html")
        for name in (
            "warehouse.html", "warehouse_analytics.html", "warehouse_brands.html",
            "warehouse_categories.html"
        ):
            source = self.source(name)
            self.assertIn("products_workspace_header", source)
            self.assertNotIn("Часы закончились", source)
        for label in ("Товары", "Бренды", "Категории", "Аналитика"):
            self.assertIn(label, workspace)
        self.assertNotIn("('products', 'В наличии'", workspace)
        self.assertNotIn("('out_of_stock', 'Нет в наличии'", workspace)

    def test_products_header_and_actions_match_compact_design(self):
        source = self.source("warehouse.html")
        workspace = self.source("_products_workspace.html")
        self.assertIn("Каталог и складские остатки", workspace)
        self.assertIn("products-workspace-metrics", workspace)
        self.assertNotIn("Экспортировать найденные", workspace)
        self.assertNotIn("Экспортировать все товары", workspace)
        self.assertIn('name="stock_state"', source)
        self.assertNotIn(">Инвентаризация</a>", workspace)
        self.assertIn("Добавить из Bitrix", workspace)
        self.assertNotIn("Только в наличии", source)

    def test_brand_and_category_lists_offer_empty_toggle_and_correct_metrics(self):
        brands = self.source("warehouse_brands.html")
        categories = self.source("warehouse_categories.html")
        self.assertIn("Показать пустые", brands)
        self.assertIn("Показать пустые", categories)
        for label in ("Позиций", "В наличии", "Единиц", "Категорий"):
            self.assertIn(label, brands)
        for label in ("Брендов", "Позиций", "В наличии", "Единиц"):
            self.assertIn(label, categories)
        self.assertNotIn('<div class="category-heading"><h2>Категории</h2>', categories)

    def test_image_layout_is_contained_and_responsive(self):
        products = self.source("warehouse.html")
        products_css = (
            ROOT / "app/static/css/warehouse.css"
        ).read_text(encoding="utf-8")
        brands = self.source("warehouse_brands.html")
        self.assertIn(
            "static_asset_url('css/warehouse.css')", products
        )
        self.assertNotIn("<style>", products)
        self.assertIn("width: 48px", products_css)
        self.assertIn("height: 48px", products_css)
        self.assertIn("object-fit: contain", products_css)
        self.assertIn("width:40px", brands)
        self.assertIn("height:40px", brands)
        self.assertIn("overflow-x: auto", products_css)

    def test_product_detail_inputs_have_accessible_names(self):
        products = self.source("warehouse.html")
        for label in (
            "Название товара", "Артикул товара", "Модель товара", "Цена товара",
            "Остаток товара", "Складская ячейка товара", "Заменить фото товара",
        ):
            self.assertIn('aria-label="{}"'.format(label), products)

    def test_product_card_preserves_field_and_action_hierarchy(self):
        products = self.source("warehouse.html")
        css = (ROOT / "app/static/css/warehouse.css").read_text(
            encoding="utf-8"
        )
        detail = products.split('id="inlineProductForm"', 1)[1].split(
            "</form>", 1
        )[0]

        self.assertIn("product-detail-card product-detail-name", detail)
        self.assertLess(detail.index("detailName"), detail.index("detailArticle"))
        self.assertLess(detail.index("detailArticle"), detail.index("detailPrice"))
        self.assertLess(detail.index("detailPrice"), detail.index("detailBrand"))
        self.assertLess(detail.index("detailBrand"), detail.index("detailCategory"))
        self.assertLess(detail.index("detailCategory"), detail.index("detailStock"))
        self.assertLess(detail.index("detailStock"), detail.index("detailCell"))

        additional = detail.split(
            'data-product-additional-settings', 1
        )[1]
        self.assertIn('id="detailModel"', additional)
        self.assertIn('data-required-strap-settings', additional)

        primary_actions = detail.index('class="product-inline-actions"')
        external_actions = detail.index(
            'class="product-inline-actions product-external-actions"'
        )
        self.assertLess(primary_actions, external_actions)
        self.assertIn('data-edit-start', detail[primary_actions:external_actions])
        self.assertIn('id="detailBitrixLink"', detail[external_actions:])
        self.assertIn('id="detailPublicProductLink"', detail[external_actions:])
        self.assertIn(
            ".product-external-actions .moysklad-link::after", css
        )
        self.assertIn('content: "↗"', css)
        self.assertIn('publicProductLink.hidden = !publicProductUrl', products)
        self.assertIn('bitrixLink.setAttribute("aria-disabled", "true")', products)

    def test_table_has_synchronized_top_scrollbar_and_mobile_overflow(self):
        products = self.source("warehouse.html")
        css = (ROOT / "app/static/css/products-workspace.css").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="warehouseTableScrollTop"', products)
        self.assertIn('id="warehouseTableScrollBody"', products)
        self.assertIn("initializeWarehouseTableScrollbars", products)
        self.assertIn("target.scrollLeft = source.scrollLeft", products)
        self.assertIn("overflow-x: auto !important", css)
        self.assertIn("contain: layout paint inline-size", css)
        self.assertIn("overflow-x: clip", css)
        self.assertIn("display: table !important", css)
        self.assertIn(".warehouse-page .warehouse-column-settings", css)
        self.assertIn("display: block !important", css)

    def test_tab_state_is_namespaced_by_compatible_view(self):
        script = (ROOT / "app/static/js/products-tabs.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("allowedParameters", script)
        self.assertIn('brands: ["q", "show_empty", "brand_id"]', script)
        self.assertIn("storagePrefix + view", script)
        self.assertIn('url.searchParams.delete("view")', script)
        self.assertIn("window.VechasuProductsTabs = lifecycle", script)
        self.assertIn("if (lifecycle.initialized)", script)
        self.assertIn('document.addEventListener("click", prepareLink)', script)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("document.open()", script)
        self.assertNotIn("document.write(", script)
        self.assertNotIn('window.addEventListener("popstate"', script)

    def test_products_tab_lifecycle_cannot_reexecute_a_full_document(self):
        script = (ROOT / "app/static/js/products-tabs.js").read_text(
            encoding="utf-8"
        )
        products = self.source("warehouse.html")

        self.assertIn("const existingLifecycle = window.VechasuProductsTabs", script)
        self.assertIn("existingLifecycle.initialize()", script)
        self.assertIn("return;", script)
        self.assertEqual(script.count('addEventListener("pointerover"'), 1)
        self.assertEqual(script.count('addEventListener("focusin"'), 1)
        self.assertEqual(script.count('addEventListener("click"'), 1)
        self.assertEqual(
            products.count("js/products-tabs.js"),
            1,
        )
        self.assertIn(
            'window.addEventListener("popstate", function()',
            products,
        )
        self.assertIn("loadWarehouseResultsUrl(url", products)

    def test_products_tab_uses_automatic_content_versioning_on_full_pages(self):
        for name in (
            "warehouse.html", "warehouse_analytics.html",
            "warehouse_brands.html", "warehouse_categories.html",
        ):
            source = self.source(name)
            self.assertIn(
                "static_asset_url('css/products-workspace.css')", source
            )
            self.assertNotIn(
                "url_for('static', filename='css/products-workspace.css')",
                source,
            )
            self.assertEqual(source.count("js/products-tabs.js"), 1, name)
            self.assertIn(
                "static_asset_url('js/products-tabs.js')", source
            )
            self.assertNotIn(
                "url_for('static', filename='js/products-tabs.js')", source
            )
        versioning = (
            ROOT / "app" / "static_assets.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Date.now", versioning)
        self.assertNotIn("random", versioning.lower())

    def test_products_analytics_uses_the_shared_page_chrome(self):
        components = (
            ROOT / "app/static/css/erp-components.css"
        ).read_text(encoding="utf-8")
        workspace = (
            ROOT / "app/static/css/products-workspace.css"
        ).read_text(encoding="utf-8")

        mobile_fallback = components.split("body:not(:is(", 1)[1].split(
            ")) .main", 1
        )[0]
        self.assertIn(".products-analytics-page", mobile_fallback)

        shared_chrome = components.split(
            "Unified page chrome for products, sales, journal and repairs.",
            1,
        )[1]
        self.assertEqual(
            shared_chrome.count(".products-analytics-page"),
            17,
        )

        analytics_main = workspace.split(
            ".products-analytics-page .main {", 1
        )[1].split("}", 1)[0]
        self.assertIn("overflow-x: clip;", analytics_main)
        self.assertNotIn("padding", analytics_main)
        self.assertNotIn("min-width", analytics_main)

    def test_products_tab_script_is_idempotent_when_loaded_three_times(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable")
        script_path = ROOT / "app/static/js/products-tabs.js"
        harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

class FakeElement {
    constructor(view, href) {
        this.dataset = {productsTab: view};
        this.href = href;
    }
    closest(selector) {
        return selector === "[data-products-tab]" ? this : null;
    }
}

const documentListeners = new Map();
const windowListeners = new Map();
const storage = new Map();
const link = new FakeElement(
    "products",
    "https://erp.test/app/products?q=stale"
);
global.Element = FakeElement;
global.document = {
    readyState: "complete",
    querySelectorAll: () => [link],
    addEventListener: (type, listener) => {
        if (!documentListeners.has(type)) documentListeners.set(type, []);
        documentListeners.get(type).push(listener);
    },
};
global.sessionStorage = {
    getItem: (key) => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value),
};
global.window = {
    location: {href: "https://erp.test/app/products?q=Casio"},
    addEventListener: (type, listener) => {
        if (!windowListeners.has(type)) windowListeners.set(type, []);
        windowListeners.get(type).push(listener);
    },
};

const source = fs.readFileSync(process.argv[1], "utf8");
for (let cycle = 0; cycle < 3; cycle += 1) {
    vm.runInThisContext(source, {filename: "products-tabs.js"});
}

for (const type of ["pointerover", "focusin", "click"]) {
    assert.strictEqual(documentListeners.get(type).length, 1, type);
}
assert.strictEqual(windowListeners.get("pageshow").length, 1);
assert.strictEqual(windowListeners.has("popstate"), false);
assert.strictEqual(window.VechasuProductsTabs.initialized, true);
documentListeners.get("click")[0]({target: link});
assert.strictEqual(
    link.href,
    "https://erp.test/app/products?q=Casio"
);
"""
        result = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_out_of_stock_checks_tolerate_an_empty_cycle(self):
        products = self.source("warehouse.html")
        self.assertIn(
            'item.out_of_stock_cycle.get("checks", {})', products
        )


if __name__ == "__main__":
    unittest.main()
