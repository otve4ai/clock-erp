(function (root, factory) {
    "use strict";

    const api = factory(root);
    if (typeof module === "object" && module.exports) {
        module.exports = api;
    }
    root.ErpNativeTableColumns = api;
})(typeof globalThis === "object" ? globalThis : this, function (root) {
    "use strict";

    const HANDLE_CLASS = "sales-column-resize-handle";
    const DROP_BEFORE_CLASS = "sales-drop-before";
    const DROP_AFTER_CLASS = "sales-drop-after";
    const INTERACTING_CLASS = "sales-table-interacting";
    const RESIZING_CLASS = "sales-column-resizing";

    function moveColumnOrder(order, sourceKey, targetKey, after) {
        if (
            sourceKey === targetKey
            || !order.includes(sourceKey)
            || !order.includes(targetKey)
        ) {
            return order.slice();
        }
        const next = order.filter((key) => key !== sourceKey);
        let targetIndex = next.indexOf(targetKey);
        if (after) targetIndex += 1;
        next.splice(targetIndex, 0, sourceKey);
        return next;
    }

    function create(options) {
        const table = options.table;
        const view = options.view;
        if (!table || !view || !Array.isArray(view.order)) {
            throw new Error("ErpNativeTableColumns requires table and view.order");
        }
        if (!root.ErpTableLayout?.computeColumnWidths) {
            throw new Error("ErpTableLayout must be loaded first");
        }

        const container = options.container || table.parentElement;
        const resizeTarget = options.resizeTarget || container;
        const systemColumnSelector = options.systemColumnSelector
            || '[data-system-column="actions"]';
        const maximumWidth = Number(options.maximumWidth) || 520;
        const normalizeOrder = options.normalizeOrder || ((order) => order.slice());
        const minimumWidthFor = options.minimumWidthFor
            || ((key) => Number(options.minimumWidths?.[key]) || 76);
        const actionWidth = typeof options.actionWidth === "function"
            ? options.actionWidth
            : () => Number(options.actionWidth) || 0;
        const signal = options.signal;
        const removers = [];
        let actualWidths = {};
        let lastContainerWidth = -1;
        let lastObservedWidth = -1;
        let layoutFrame = 0;
        let layoutObserver = null;
        let suppressClickUntil = 0;

        function listen(target, type, listener, listenerOptions) {
            const eventOptions = Object.assign({}, listenerOptions || {});
            if (signal) eventOptions.signal = signal;
            target.addEventListener(type, listener, eventOptions);
            if (!signal) {
                removers.push(() => target.removeEventListener(
                    type,
                    listener,
                    eventOptions,
                ));
            }
        }

        function visibleKeys() {
            return view.order.filter((key) => !view.hidden.includes(key));
        }

        function applyOrder() {
            view.order = normalizeOrder(view.order);
            table.querySelectorAll("tr").forEach((row) => {
                view.order.forEach((key) => {
                    const cell = row.querySelector(
                        '[data-column-key="' + key + '"]',
                    );
                    if (cell) row.appendChild(cell);
                });
                const systemColumn = row.querySelector(systemColumnSelector);
                if (systemColumn) row.appendChild(systemColumn);
            });
            const colgroup = table.querySelector("colgroup");
            if (colgroup) {
                view.order.forEach((key) => {
                    const column = colgroup.querySelector(
                        '[data-column-key="' + key + '"]',
                    );
                    if (column) colgroup.appendChild(column);
                });
                const systemColumn = colgroup.querySelector(systemColumnSelector);
                if (systemColumn) colgroup.appendChild(systemColumn);
            }
            return view.order;
        }

        function applyLayout(force) {
            const keys = visibleKeys();
            const containerWidth = container?.clientWidth || 0;
            if (
                !force
                && Math.abs(containerWidth - lastContainerWidth) < 0.5
            ) {
                return actualWidths;
            }
            lastContainerWidth = containerWidth;
            const fixedWidths = Array.isArray(view.customWidths)
                && view.customWidths.length > 0;
            const layout = fixedWidths
                ? {
                    widths: Object.fromEntries(
                        keys.map((key) => [key, view.widths[key]]),
                    ),
                    tableWidth: keys.reduce(
                        (total, key) => total + view.widths[key],
                        actionWidth(),
                    ),
                }
                : root.ErpTableLayout.computeColumnWidths({
                    keys,
                    preferredWidths: view.widths,
                    minimumWidths: options.minimumWidths || {},
                    growWeights: options.growWeights || {},
                    containerWidth,
                    actionWidth: actionWidth(),
                });
            actualWidths = layout.widths;

            const actionColumn = table.querySelector(
                "col" + systemColumnSelector,
            );
            if (actionColumn) {
                actionColumn.style.width = actionWidth() + "px";
            }
            view.order.forEach((key) => {
                const hidden = view.hidden.includes(key);
                const width = hidden ? view.widths[key] : actualWidths[key];
                table.querySelectorAll(
                    '[data-column-key="' + key + '"]',
                ).forEach((element) => {
                    element.hidden = hidden;
                    if (element.tagName === "COL") {
                        element.style.width = width + "px";
                    }
                });
            });
            const tableWidth = layout.tableWidth + "px";
            const exactPriority = fixedWidths ? "important" : "";
            table.style.setProperty("width", tableWidth, exactPriority);
            table.style.setProperty("min-width", tableWidth, exactPriority);
            table.dataset.horizontalOverflow = String(
                layout.tableWidth > containerWidth + 0.5,
            );
            options.onLayout?.({
                widths: {...actualWidths},
                tableWidth: layout.tableWidth,
                containerWidth,
            });
            return actualWidths;
        }

        function applyView(force) {
            applyOrder();
            return applyLayout(force !== false);
        }

        function scheduleLayout(force) {
            if (layoutFrame) root.cancelAnimationFrame(layoutFrame);
            layoutFrame = root.requestAnimationFrame(() => {
                layoutFrame = 0;
                applyLayout(Boolean(force));
            });
        }

        function notifyChange(type) {
            options.onChange?.({type, view, widths: {...actualWidths}});
        }

        function suppressSort() {
            suppressClickUntil = Date.now() + 350;
            options.onSuppressSort?.(suppressClickUntil);
        }

        function snapshotVisibleWidths() {
            if (!Array.isArray(view.customWidths)) view.customWidths = [];
            visibleKeys().forEach((key) => {
                const header = table.querySelector(
                    'thead th[data-column-key="' + key + '"]',
                );
                const measured = header?.getBoundingClientRect().width;
                view.widths[key] = Number.isFinite(measured) && measured > 0
                    ? measured
                    : actualWidths[key];
                if (!view.customWidths.includes(key)) {
                    view.customWidths.push(key);
                }
            });
        }

        table.querySelectorAll("thead th[data-column-key]").forEach((header) => {
            const existing = header.querySelector("." + HANDLE_CLASS);
            if (existing) existing.remove();
            const handle = document.createElement("span");
            handle.className = HANDLE_CLASS;
            handle.setAttribute("aria-hidden", "true");
            header.appendChild(handle);
            listen(handle, "pointerdown", (event) => {
                event.preventDefault();
                event.stopPropagation();
                const key = header.dataset.columnKey;
                const startX = event.clientX;
                const startWidth = header.getBoundingClientRect().width;
                snapshotVisibleWidths();
                handle.classList.add("is-active");
                document.body.classList.add(INTERACTING_CLASS, RESIZING_CLASS);

                function resizeColumn(moveEvent) {
                    view.widths[key] = Math.min(
                        maximumWidth,
                        Math.max(
                            minimumWidthFor(key),
                            Math.round(startWidth + moveEvent.clientX - startX),
                        ),
                    );
                    applyLayout(true);
                }

                function finishResize() {
                    suppressSort();
                    handle.classList.remove("is-active");
                    document.body.classList.remove(
                        INTERACTING_CLASS,
                        RESIZING_CLASS,
                    );
                    notifyChange("resize");
                    root.removeEventListener("pointermove", resizeColumn);
                    root.removeEventListener("pointerup", finishResize);
                    root.removeEventListener("pointercancel", finishResize);
                }

                root.addEventListener("pointermove", resizeColumn);
                root.addEventListener("pointerup", finishResize, {once: true});
                root.addEventListener("pointercancel", finishResize, {once: true});
            });
        });

        function clearDropMarkers() {
            table.querySelectorAll(
                "." + DROP_BEFORE_CLASS + ", ." + DROP_AFTER_CLASS,
            ).forEach((header) => {
                header.classList.remove(DROP_BEFORE_CLASS, DROP_AFTER_CLASS);
            });
        }

        let dragState = null;
        listen(table.querySelector("thead"), "pointerdown", (event) => {
            if (
                event.button !== 0
                || event.target.closest("." + HANDLE_CLASS)
            ) {
                return;
            }
            const header = event.target.closest("th[data-column-key]");
            if (!header || header.hidden) return;
            dragState = {
                header,
                startX: event.clientX,
                dragging: false,
                target: null,
                after: false,
                preview: null,
            };
        });
        listen(root, "pointermove", (event) => {
            if (!dragState) return;
            if (
                !dragState.dragging
                && Math.abs(event.clientX - dragState.startX) < 6
            ) {
                return;
            }
            if (!dragState.dragging) {
                dragState.dragging = true;
                document.body.classList.add(INTERACTING_CLASS);
                dragState.header.classList.add("erp-column-dragging");
                dragState.preview = document.createElement("div");
                dragState.preview.className = "erp-column-drag-preview";
                dragState.preview.textContent = dragState.header.textContent.trim();
                document.body.appendChild(dragState.preview);
            }
            dragState.preview.style.left = event.clientX + 14 + "px";
            dragState.preview.style.top = event.clientY + 14 + "px";
            clearDropMarkers();
            const element = document.elementFromPoint(event.clientX, event.clientY);
            const target = element
                ? element.closest("th[data-column-key]")
                : null;
            if (
                !target
                || target.closest("table") !== table
                || target === dragState.header
                || target.hidden
            ) {
                dragState.target = null;
                return;
            }
            const bounds = target.getBoundingClientRect();
            dragState.target = target;
            dragState.after = event.clientX > bounds.left + bounds.width / 2;
            target.classList.add(
                dragState.after ? DROP_AFTER_CLASS : DROP_BEFORE_CLASS,
            );
        });

        function finishColumnDrag() {
            if (!dragState) return;
            if (dragState.dragging) {
                suppressSort();
                if (dragState.target) {
                    view.order = normalizeOrder(moveColumnOrder(
                        view.order,
                        dragState.header.dataset.columnKey,
                        dragState.target.dataset.columnKey,
                        dragState.after,
                    ));
                    applyView(true);
                    notifyChange("order");
                }
                clearDropMarkers();
                document.body.classList.remove(INTERACTING_CLASS);
                dragState.header.classList.remove("erp-column-dragging");
                dragState.preview?.remove();
            }
            dragState = null;
        }
        listen(root, "pointerup", finishColumnDrag);
        listen(root, "pointercancel", finishColumnDrag);
        listen(table, "click", (event) => {
            if (Date.now() < suppressClickUntil) {
                event.preventDefault();
                event.stopPropagation();
            }
        }, {capture: true});

        if (typeof root.ResizeObserver === "function" && resizeTarget) {
            layoutObserver = new root.ResizeObserver((entries) => {
                const observedWidth = entries[0]?.contentRect.width || 0;
                if (Math.abs(observedWidth - lastObservedWidth) < 0.5) return;
                lastObservedWidth = observedWidth;
                scheduleLayout(false);
            });
            layoutObserver.observe(resizeTarget);
        }
        listen(root, "resize", () => scheduleLayout(false));
        const layoutRoot = table.closest("[data-erp-focus-mode]");
        if (layoutRoot) {
            listen(layoutRoot, "erp:focus-mode-change", () => scheduleLayout(true));
            listen(layoutRoot, "erp:sidebar-change", () => scheduleLayout(true));
        }
        listen(root, "pagehide", () => {
            layoutObserver?.disconnect();
        });
        listen(root, "pageshow", () => {
            if (layoutObserver && resizeTarget) {
                layoutObserver.observe(resizeTarget);
            }
            scheduleLayout(true);
        });
        if (signal) {
            signal.addEventListener("abort", () => {
                layoutObserver?.disconnect();
                if (layoutFrame) root.cancelAnimationFrame(layoutFrame);
            }, {once: true});
        }

        function moveColumn(sourceKey, targetKey, after) {
            const next = moveColumnOrder(
                view.order,
                sourceKey,
                targetKey,
                after,
            );
            if (next.join("\u0000") === view.order.join("\u0000")) return;
            view.order = normalizeOrder(next);
            applyView(true);
            notifyChange("order");
        }

        function destroy() {
            layoutObserver?.disconnect();
            if (layoutFrame) root.cancelAnimationFrame(layoutFrame);
            removers.forEach((remove) => remove());
            table.querySelectorAll("." + HANDLE_CLASS).forEach((handle) => {
                handle.remove();
            });
        }

        return {
            applyOrder,
            applyLayout,
            applyView,
            scheduleLayout,
            moveColumn,
            getActualWidths: () => ({...actualWidths}),
            destroy,
        };
    }

    return {create, moveColumnOrder};
});
