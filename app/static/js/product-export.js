(function () {
    "use strict";

    function filterLabels(currentState) {
        var params = new URLSearchParams(window.location.search);
        var labels = [];
        var query = params.get("q");
        var brand = currentState?.brand || params.get("brand");
        var category = currentState?.category || params.get("category");
        var model = currentState?.model || params.get("model");
        var cell = currentState?.cell || params.get("cell");
        var stock = currentState?.stock_state || params.get("stock_state");
        var siteIssue = currentState?.site_issue || params.get("site_issue");
        var view = params.get("view");
        if (query) labels.push("Поиск: " + query);
        if (params.get("brand_id") === "0") labels.push("Бренд: Без бренда");
        else if (brand) labels.push("Бренд: " + brand);
        if (params.get("category_id") === "0") labels.push("Категория: Без категории");
        else if (category) labels.push("Категория: " + category);
        if (model) labels.push("Модель: " + model);
        if (cell) labels.push("Ячейка: " + cell);
        if (params.get("date_from") || params.get("date_to")) {
            labels.push("Период: " + (params.get("date_from") || "…") + " — " + (params.get("date_to") || "…"));
        }
        if (view === "out_of_stock") stock = "out";
        if (view === "in_stock") stock = "in";
        if (stock === "in") labels.push("Наличие: В наличии");
        if (stock === "out") labels.push("Наличие: Нет в наличии");
        if (stock === "out" && params.get("check_state") && params.get("check_state") !== "all") {
            labels.push("Статус проверки: " + params.get("check_state"));
        }
        if (siteIssue === "in_stock_inactive") {
            labels.push("Статус сайта: С остатком выключены");
        }
        if (siteIssue === "out_of_stock_active") {
            labels.push("Статус сайта: Без остатка активны");
        }
        return labels;
    }

    function syncFilters(currentState) {
        var container = document.querySelector("[data-export-current-filters]");
        if (!container) return;
        var labels = filterLabels(currentState);
        container.replaceChildren();
        container.classList.toggle("is-empty", labels.length === 0);
        if (!labels.length) {
            container.textContent = "Фильтры не применены — выборка совпадает со всем каталогом.";
            return;
        }
        var strong = document.createElement("strong");
        strong.textContent = "Сейчас применены фильтры:";
        container.appendChild(strong);
        labels.forEach(function (label) {
            var span = document.createElement("span");
            span.textContent = label;
            container.appendChild(span);
        });
    }

    function notify(message, error) {
        var toast = document.getElementById("productExportToast");
        if (!toast) return;
        toast.textContent = message;
        toast.classList.toggle("is-error", Boolean(error));
        toast.hidden = false;
        window.clearTimeout(notify.timer);
        notify.timer = window.setTimeout(function () { toast.hidden = true; }, 6000);
    }

    function closeMenus(except) {
        ["productsActionsMenu", "productsAddMenu"].forEach(function (id) {
            var menu = document.getElementById(id);
            if (menu && menu !== except) menu.hidden = true;
        });
    }

    window.toggleProductsActionsMenu = function (event) {
        event.preventDefault();
        event.stopPropagation();
        var menu = document.getElementById("productsActionsMenu");
        if (!menu) return;
        closeMenus(menu);
        menu.hidden = !menu.hidden;
        event.currentTarget.setAttribute("aria-expanded", String(!menu.hidden));
    };

    document.addEventListener("click", function (event) {
        if (event.target.closest("#openProductExport")) {
            var dialog = document.getElementById("productExportDialog");
            closeMenus();
            syncFilters();
            if (dialog && !dialog.open) dialog.showModal();
            return;
        }
        if (event.target.closest("[data-export-close]")) {
            document.getElementById("productExportDialog")?.close();
            return;
        }
        if (event.target.closest("[data-export-fields-all]")) {
            document.querySelectorAll('#productExportForm [name="fields"]').forEach(function (input) { input.checked = true; });
            return;
        }
        if (event.target.closest("[data-export-fields-none]")) {
            document.querySelectorAll('#productExportForm [name="fields"]').forEach(function (input) { input.checked = false; });
            return;
        }
        if (!event.target.closest(".products-actions-menu")) closeMenus();
    });

    document.addEventListener("warehouse:results-updated", function (event) {
        var count = document.querySelector("[data-export-filtered-count]");
        if (count) count.textContent = Number(event.detail?.total || 0) + " товаров";
        syncFilters(event.detail);
    });

    document.addEventListener("DOMContentLoaded", function () {
        var form = document.getElementById("productExportForm");
        if (!form) return;
        form.addEventListener("submit", async function (event) {
            event.preventDefault();
            var error = form.querySelector("[data-export-error]");
            var fields = form.querySelectorAll('[name="fields"]:checked');
            if (!fields.length) {
                error.textContent = "Выберите хотя бы одно поле для экспорта.";
                return;
            }
            error.textContent = "";
            var submit = form.querySelector("[data-export-submit]");
            submit.disabled = true;
            submit.textContent = "Формируем…";
            try {
                var data = new FormData(form);
                var params = new URLSearchParams(window.location.search);
                ["page", "per_page", "scope", "fields", "selected_ids", "filename"].forEach(function (key) { params.delete(key); });
                params.forEach(function (value, key) { data.set(key, value); });
                var response = await fetch(form.action, {
                    method: "POST",
                    body: data,
                    credentials: "same-origin",
                    headers: {"Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/json"}
                });
                if (!response.ok) {
                    var payload = await response.json().catch(function () { return {}; });
                    throw new Error(payload.message || "Не удалось сформировать Excel-файл.");
                }
                var blob = await response.blob();
                var filename = String(form.elements.filename.value || "Товары.xlsx").trim();
                if (!filename.toLowerCase().endsWith(".xlsx")) filename += ".xlsx";
                var link = document.createElement("a");
                link.href = URL.createObjectURL(blob);
                link.download = filename;
                document.body.appendChild(link);
                link.click();
                link.remove();
                window.setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
                var count = Number(response.headers.get("X-Export-Count") || 0);
                document.getElementById("productExportDialog")?.close();
                notify("Экспорт завершён. Файл «" + filename + "» успешно сформирован — " + count + " товаров.");
            } catch (fetchError) {
                error.textContent = fetchError.message;
                notify(fetchError.message, true);
            } finally {
                submit.disabled = false;
                submit.textContent = "Скачать Excel";
            }
        });
    });
}());
