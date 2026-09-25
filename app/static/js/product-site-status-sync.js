(function () {
    "use strict";

    var root = document.querySelector("[data-product-site-sync]");
    if (!root) {
        return;
    }

    var toggle = root.querySelector("[data-site-sync-toggle]");
    var panel = root.querySelector("[data-site-sync-panel]");
    var label = root.querySelector("[data-site-sync-indicator-label]");
    var state = root.querySelector("[data-site-sync-state]");
    var attempt = root.querySelector("[data-site-sync-attempt]");
    var success = root.querySelector("[data-site-sync-success]");
    var feedback = root.querySelector("[data-site-sync-feedback]");
    var inactive = root.querySelector("[data-site-sync-inactive]");
    var active = root.querySelector("[data-site-sync-active]");
    var running = false;

    function formatDate(value) {
        if (!value) {
            return "Нет данных";
        }
        var parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) {
            return value;
        }
        return new Intl.DateTimeFormat("ru-RU", {
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit"
        }).format(parsed);
    }

    function setTime(node, value) {
        if (!node) {
            return;
        }
        if (value) {
            node.setAttribute("datetime", value);
        } else {
            node.removeAttribute("datetime");
        }
        node.textContent = formatDate(value);
    }

    function applySummary(summary) {
        var inStockInactive = Number(summary.in_stock_inactive || 0);
        var outOfStockActive = Number(summary.out_of_stock_active || 0);
        var mismatchCount = inStockInactive + outOfStockActive;
        var hasData = Boolean(summary.has_data || summary.last_success_at);
        var outcome = summary.outcome || "unknown";
        root.dataset.syncState = (
            outcome === "running" || outcome === "error" ? outcome
                : !hasData ? "unknown"
                    : mismatchCount > 0 ? "attention" : "success"
        );
        inactive.textContent = String(inStockInactive);
        active.textContent = String(outOfStockActive);
        setTime(attempt, summary.last_attempt_at);

        if (outcome === "running" || running) {
            label.textContent = "Проверка…";
            state.textContent = "Проверка выполняется";
        } else if (outcome === "error") {
            label.textContent = "Ошибка сверки";
            state.textContent = "Ошибка сверки";
        } else if (!hasData) {
            label.textContent = "Нет данных";
            state.textContent = "Нет данных";
        } else if (mismatchCount > 0) {
            label.textContent = "Сверка сайта · " + mismatchCount + " расхождений";
            state.textContent = "Требует внимания";
        } else {
            label.textContent = "Сверка сайта · Всё совпадает";
            state.textContent = "Всё совпадает";
        }

        if (summary.last_success_at) {
            if (!success) {
                feedback.textContent = "Последняя сверка: ";
                success = document.createElement("time");
                success.dataset.siteSyncSuccess = "";
                feedback.appendChild(success);
            }
            setTime(success, summary.last_success_at);
        } else {
            feedback.textContent = "Последняя сверка: нет данных";
            success = null;
        }
    }

    function closePanel(options) {
        if (panel.hidden) {
            return;
        }
        panel.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
        root.classList.remove("is-open");
        if (options && options.focusToggle) {
            toggle.focus();
        }
    }

    function openPanel() {
        panel.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
        root.classList.add("is-open");
    }

    async function fetchSummary() {
        var response = await fetch(root.dataset.summaryUrl, {
            credentials: "same-origin",
            headers: {"Accept": "application/json"}
        });
        var payload = await response.json();
        if (!response.ok) {
            throw new Error((payload && payload.message) || "Не удалось обновить данные");
        }
        applySummary(payload.data || {});
    }

    async function runSync() {
        if (running) {
            return;
        }
        running = true;
        root.dataset.syncState = "running";
        label.textContent = "Проверка…";
        state.textContent = "Проверка выполняется";
        root.querySelectorAll("[data-site-sync-run]").forEach(function (button) {
            button.disabled = true;
        });
        try {
            var response = await fetch(root.dataset.runUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Accept": "application/json",
                    "X-CSRF-Token": root.dataset.csrfToken || ""
                }
            });
            var payload = await response.json();
            if (!response.ok) {
                throw new Error((payload && payload.message) || "Ошибка сверки");
            }
            running = false;
            applySummary(payload.data || {});
        } catch (error) {
            running = false;
            root.dataset.syncState = "error";
            label.textContent = "Ошибка сверки";
            state.textContent = "Ошибка сверки";
            feedback.textContent = error.message;
        } finally {
            root.querySelectorAll("[data-site-sync-run]").forEach(function (button) {
                button.disabled = false;
            });
        }
    }

    root.querySelectorAll("time[datetime]").forEach(function (node) {
        setTime(node, node.getAttribute("datetime"));
    });

    toggle.addEventListener("click", function () {
        if (panel.hidden) {
            openPanel();
        } else {
            closePanel();
        }
    });
    root.querySelectorAll("[data-site-sync-run]").forEach(function (button) {
        button.addEventListener("click", runSync);
    });
    root.querySelectorAll("[data-site-sync-refresh]").forEach(function (button) {
        button.addEventListener("click", function () {
            fetchSummary().catch(function (error) {
                feedback.textContent = error.message;
            });
            var menu = button.closest("details");
            if (menu) {
                menu.open = false;
            }
        });
    });
    document.addEventListener("click", function (event) {
        if (!panel.hidden && !root.contains(event.target)) {
            closePanel();
        }
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !panel.hidden) {
            closePanel({focusToggle: true});
        }
    });

    window.setInterval(function () {
        if (!running && !document.hidden) {
            fetchSummary().catch(function () {});
        }
    }, 60000);
}());
