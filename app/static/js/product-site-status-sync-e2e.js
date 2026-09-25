(function () {
    "use strict";

    window.addEventListener("DOMContentLoaded", async function () {
        var params = new URLSearchParams(window.location.search);
        var mode = params.get("site_status_sync_e2e");
        var root = document.querySelector("[data-product-site-sync]");
        if (!root) {
            document.documentElement.dataset.siteStatusSyncE2e = "fail-missing-root";
            return;
        }
        var toggle = root.querySelector("[data-site-sync-toggle]");
        var panel = root.querySelector("[data-site-sync-panel]");
        if (mode === "open") {
            toggle.click();
            document.documentElement.dataset.siteStatusSyncE2e =
                panel.hidden ? "fail-open" : "pass";
            return;
        }
        if (mode !== "behavior") {
            return;
        }

        var originalFetch = window.fetch;
        var postRequests = 0;
        var getRequests = 0;
        var postMode = "success";
        window.fetch = async function (_url, options) {
            if ((options && options.method) === "POST") {
                postRequests += 1;
                if (postMode === "error") {
                    return {
                        ok: false,
                        json: async function () {
                            return {message: "Контрольная ошибка"};
                        }
                    };
                }
                await new Promise(function (resolve) {
                    window.setTimeout(resolve, 40);
                });
                return {
                    ok: true,
                    json: async function () {
                        return {data: {
                            outcome: "success",
                            has_data: true,
                            in_stock_inactive: 0,
                            out_of_stock_active: 0,
                            last_attempt_at: "2026-09-25T10:00:00+00:00",
                            last_success_at: "2026-09-25T10:00:00+00:00"
                        }};
                    }
                };
            }
            getRequests += 1;
            return {
                ok: true,
                json: async function () {
                    return {data: {
                        outcome: "unknown",
                        has_data: false,
                        in_stock_inactive: 0,
                        out_of_stock_active: 0,
                        last_attempt_at: null,
                        last_success_at: null
                    }};
                }
            };
        };

        var result = {};
        try {
            result.initialCount = toggle.textContent.includes("46 расхождений");
            toggle.click();
            result.openWithoutRun = !panel.hidden && postRequests === 0;
            toggle.click();
            result.repeatCloses = panel.hidden;
            toggle.click();
            document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape"}));
            result.escapeCloses = panel.hidden;
            toggle.click();
            document.body.dispatchEvent(new MouseEvent("click", {bubbles: true}));
            result.outsideCloses = panel.hidden;

            toggle.click();
            var runButton = root.querySelector("[data-site-sync-run]");
            runButton.click();
            result.runningState = toggle.textContent.includes("Проверка…");
            await new Promise(function (resolve) {
                window.setTimeout(resolve, 80);
            });
            result.manualSuccess = postRequests === 1
                && toggle.textContent.includes("Всё совпадает");

            var refreshButton = root.querySelector("[data-site-sync-refresh]");
            refreshButton.click();
            await new Promise(function (resolve) {
                window.setTimeout(resolve, 20);
            });
            result.noDataState = getRequests === 1
                && toggle.textContent.includes("Нет данных");

            postMode = "error";
            runButton.click();
            await new Promise(function (resolve) {
                window.setTimeout(resolve, 20);
            });
            result.errorState = toggle.textContent.includes("Ошибка сверки");
            document.documentElement.dataset.siteStatusSyncE2e =
                Object.values(result).every(Boolean) ? "pass" : "fail";
        } catch (error) {
            document.documentElement.dataset.siteStatusSyncE2e =
                "fail-" + error.message;
        } finally {
            document.documentElement.dataset.siteStatusSyncE2eResult =
                JSON.stringify(result);
            window.fetch = originalFetch;
        }
    });
}());
