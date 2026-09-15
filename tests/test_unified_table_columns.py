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


def test_existing_sales_and_warehouse_tables_keep_independent_widths_and_preview():
    sales = (TEMPLATES / "sales.html").read_text(encoding="utf-8")
    warehouse = (TEMPLATES / "warehouse.html").read_text(encoding="utf-8")

    assert "view.widths[columnKey] = actualWidths[columnKey]" in sales
    assert 'table.style.minWidth = renderedTotal + "px"' in warehouse
    assert "flexibleColumns" not in warehouse
    assert 'preview.className = "erp-column-drag-preview"' in sales
    assert 'preview.className = "erp-column-drag-preview"' in warehouse
