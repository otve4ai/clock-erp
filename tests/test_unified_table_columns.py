from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


def test_every_erp_table_receives_column_controls_or_is_print_only():
    table_templates = {
        path.name: path.read_text(encoding="utf-8")
        for path in TEMPLATES.glob("*.html")
        if "<table" in path.read_text(encoding="utf-8")
    }
    partials_loaded_by_managed_pages = {
        "_analytics_section.html",
        "_orders_list_results.html",
        "_wb_recovery.html",
        "_writeoffs.html",
    }

    uncovered = []
    for name, source in table_templates.items():
        covered = (
            '_sidebar.html' in source
            or 'erp-table-columns.js' in source
            or name in partials_loaded_by_managed_pages
            or 'data-erp-column-controls="off"' in source
        )
        if not covered:
            uncovered.append(name)

    assert uncovered == []
    assert 'data-erp-column-controls="off"' in table_templates["order_print.html"]


def test_shared_column_controller_keeps_order_and_widths_per_table():
    source = (ROOT / "app" / "static" / "js" / "erp-table-columns.js").read_text(
        encoding="utf-8"
    )

    assert 'STORAGE_PREFIX = "vechasu:erp-table-columns:v1:"' in source
    assert 'order: context.order' in source
    assert 'widths: context.widths' in source
    assert 'context.widths[key]' in source
    assert 'context.table.style.width' in source
    assert 'erp-column-drop-before' in source
    assert 'erp-column-drop-after' in source
    assert 'erp-column-drag-preview' in source
    assert 'MutationObserver' in source


def test_shared_controller_is_loaded_once_from_the_erp_shell():
    sidebar = (TEMPLATES / "_sidebar.html").read_text(encoding="utf-8")

    assert sidebar.count("erp-table-columns.css") == 1
    assert sidebar.count("erp-table-columns.js") == 1


def test_sales_and_warehouse_tables_share_the_same_native_interaction_contract():
    sales = (TEMPLATES / "sales.html").read_text(encoding="utf-8")
    warehouse = (TEMPLATES / "warehouse.html").read_text(encoding="utf-8")
    controller = (
        ROOT / "app" / "static" / "js" / "erp-native-table-columns.js"
    ).read_text(encoding="utf-8")

    for template in (sales, warehouse):
        assert template.count("js/erp-native-table-columns.js") == 1
        assert "window.ErpNativeTableColumns.create({" in template
        assert 'handle.className = "sales-column-resize-handle"' not in template
        assert "let dragState = null" not in template

    assert "snapshotVisibleWidths();" in controller
    assert "function moveColumnOrder" in controller
    assert "function applyLayout" in controller
    assert 'const HANDLE_CLASS = "sales-column-resize-handle"' in controller
    assert 'data-system-column="actions"' in warehouse
    assert "Math.abs(event.clientX - dragState.startX) < 6" in controller
    assert 'dragState.preview.className = "erp-column-drag-preview"' in controller
