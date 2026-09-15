(function (root) {
    "use strict";

    const STORAGE_PREFIX = "vechasu:erp-table-columns:v1:";
    const MINIMUM_WIDTH = 56;
    const MAXIMUM_WIDTH = 1200;
    const managed = new WeakMap();
    let scanQueued = false;

    function normalizeLabel(value) {
        return String(value || "")
            .replace(/\s+/g, " ")
            .trim()
            .toLocaleLowerCase("ru-RU")
            .replace(/[^a-zа-яё0-9]+/gi, "-")
            .replace(/^-|-$/g, "") || "column";
    }

    function hash(value) {
        let result = 5381;
        for (let index = 0; index < value.length; index += 1) {
            result = ((result << 5) + result) ^ value.charCodeAt(index);
        }
        return (result >>> 0).toString(36);
    }

    function headerRow(table) {
        const rows = table.tHead ? Array.from(table.tHead.rows) : [];
        if (rows.length !== 1) return null;
        const row = rows[0];
        const cells = Array.from(row.cells);
        if (cells.length < 2 || cells.some((cell) => cell.colSpan !== 1 || cell.rowSpan !== 1)) {
            return null;
        }
        return row;
    }

    function buildKeys(headers) {
        const counts = new Map();
        return headers.map((header) => {
            const explicit = header.dataset.columnKey || header.dataset.column || "";
            const base = normalizeLabel(explicit || header.textContent);
            const count = (counts.get(base) || 0) + 1;
            counts.set(base, count);
            return count === 1 ? base : base + "-" + count;
        });
    }

    function tableIdentity(table, keys) {
        const explicit = table.dataset.erpTableKey || table.id;
        const semantic = explicit || keys.slice().sort().join("|");
        const pathname = root.location ? root.location.pathname : "test";
        const peers = Array.from(document.querySelectorAll("table")).filter((candidate) => {
            if (candidate === table) return true;
            const row = headerRow(candidate);
            return row && buildKeys(Array.from(row.cells)).slice().sort().join("|") === keys.slice().sort().join("|");
        });
        const occurrence = Math.max(0, peers.indexOf(table));
        return STORAGE_PREFIX + hash(pathname + "|" + semantic + "|" + occurrence);
    }

    function readState(storageKey, keys) {
        let saved = {};
        try {
            saved = JSON.parse(root.localStorage.getItem(storageKey) || "{}") || {};
        } catch (_error) {
            saved = {};
        }
        const order = [];
        (Array.isArray(saved.order) ? saved.order : []).forEach((key) => {
            if (keys.includes(key) && !order.includes(key)) order.push(key);
        });
        keys.forEach((key) => {
            if (!order.includes(key)) order.push(key);
        });
        const widths = {};
        if (saved.widths && typeof saved.widths === "object") {
            keys.forEach((key) => {
                const width = Number(saved.widths[key]);
                if (Number.isFinite(width)) {
                    widths[key] = Math.max(MINIMUM_WIDTH, Math.min(MAXIMUM_WIDTH, Math.round(width)));
                }
            });
        }
        return {order, widths};
    }

    function saveState(context) {
        try {
            root.localStorage.setItem(context.storageKey, JSON.stringify({
                order: context.order,
                widths: context.widths,
            }));
        } catch (_error) {
            // The table remains usable when browser storage is unavailable.
        }
    }

    function isNativeManaged(table) {
        return table.id === "warehouseProductsTable" || Boolean(table.dataset.salesSettingsKey);
    }

    function ensureScrollContainer(table) {
        let candidate = table.parentElement;
        let current = candidate;
        while (current && current !== document.body) {
            const overflow = root.getComputedStyle(current).overflowX;
            if (overflow === "auto" || overflow === "scroll") {
                candidate = current;
                break;
            }
            current = current.parentElement;
        }
        if (candidate) candidate.classList.add("erp-column-table-scroll");
        return candidate;
    }

    function cellsForRow(row, count) {
        const cells = Array.from(row.cells);
        if (cells.length !== count || cells.some((cell) => cell.colSpan !== 1 || cell.rowSpan !== 1)) {
            return null;
        }
        return cells;
    }

    function tagCells(table, keys) {
        const allRows = [];
        Array.from(table.tBodies).forEach((section) => allRows.push(...section.rows));
        if (table.tFoot) allRows.push(...table.tFoot.rows);
        allRows.forEach((row) => {
            const cells = cellsForRow(row, keys.length);
            if (!cells) return;
            cells.forEach((cell, index) => {
                if (!cell.dataset.erpColumnKey) cell.dataset.erpColumnKey = keys[index];
            });
        });
    }

    function ensureColgroup(table, keys) {
        let group = table.querySelector(":scope > colgroup");
        if (!group) {
            group = document.createElement("colgroup");
            table.insertBefore(group, table.firstChild);
        }
        const columns = Array.from(group.children).filter((node) => node.tagName === "COL");
        while (columns.length < keys.length) {
            const column = document.createElement("col");
            group.appendChild(column);
            columns.push(column);
        }
        columns.slice(0, keys.length).forEach((column, index) => {
            column.dataset.erpColumnKey = keys[index];
        });
        return group;
    }

    function applyOrder(context, requestedOrder) {
        const {table, keys} = context;
        const order = requestedOrder || context.order;
        const rows = [context.header];
        Array.from(table.tBodies).forEach((section) => rows.push(...section.rows));
        if (table.tFoot) rows.push(...table.tFoot.rows);
        rows.forEach((row) => {
            const cells = cellsForRow(row, keys.length);
            if (!cells) return;
            const current = cells.map((cell) => cell.dataset.erpColumnKey);
            if (order.every((key, index) => current[index] === key)) return;
            order.forEach((key) => {
                const cell = cells.find((item) => item.dataset.erpColumnKey === key);
                if (cell) row.appendChild(cell);
            });
        });
        if (context.colgroup) {
            const columns = Array.from(context.colgroup.children);
            const managedColumns = columns.filter((item) => item.dataset.erpColumnKey);
            const current = managedColumns.map((item) => item.dataset.erpColumnKey);
            if (order.every((key, index) => current[index] === key)) return;
            order.forEach((key) => {
                const column = columns.find((item) => item.dataset.erpColumnKey === key);
                if (column) context.colgroup.appendChild(column);
            });
            columns.filter((item) => !item.dataset.erpColumnKey).forEach((item) => context.colgroup.appendChild(item));
        }
    }

    function visibleWidth(context) {
        return context.order.reduce((total, key) => {
            const header = context.header.querySelector('[data-erp-column-key="' + CSS.escape(key) + '"]');
            const hidden = header && (
                header.hidden || root.getComputedStyle(header).display === "none"
            );
            return total + (hidden ? 0 : (context.widths[key] || MINIMUM_WIDTH));
        }, 0);
    }

    function applyWidths(context) {
        if (!context.resize) return;
        context.order.forEach((key) => {
            const column = context.colgroup.querySelector('[data-erp-column-key="' + CSS.escape(key) + '"]');
            if (column) column.style.width = context.widths[key] + "px";
        });
        const width = visibleWidth(context);
        context.table.style.width = Math.ceil(width) + "px";
        context.table.style.minWidth = Math.ceil(width) + "px";
    }

    function clearDrop(context) {
        context.header.querySelectorAll(".erp-column-drop-before,.erp-column-drop-after").forEach((node) => {
            node.classList.remove("erp-column-drop-before", "erp-column-drop-after");
        });
    }

    function installDrag(context) {
        let drag = null;
        context.header.addEventListener("pointerdown", (event) => {
            if (event.button !== 0 || event.target.closest("input,select,textarea,a,label,.erp-column-resize-handle,.receipt-column-resize-handle")) return;
            const header = event.target.closest("th[data-erp-column-key]");
            if (!header || header.hidden) return;
            drag = {header, startX: event.clientX, startY: event.clientY, active: false, target: null, after: false, preview: null};
        });
        root.addEventListener("pointermove", (event) => {
            if (!drag) return;
            if (!drag.active && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 6) return;
            if (!drag.active) {
                drag.active = true;
                drag.header.classList.add("erp-column-dragging");
                document.body.classList.add("erp-column-table-interacting");
                drag.preview = document.createElement("div");
                drag.preview.className = "erp-column-drag-preview";
                drag.preview.textContent = drag.header.textContent.trim();
                document.body.appendChild(drag.preview);
            }
            event.preventDefault();
            drag.preview.style.left = event.clientX + 14 + "px";
            drag.preview.style.top = event.clientY + 14 + "px";
            clearDrop(context);
            const element = document.elementFromPoint(event.clientX, event.clientY);
            const target = element ? element.closest("th[data-erp-column-key]") : null;
            if (!target || target.closest("table") !== context.table || target === drag.header || target.hidden) {
                drag.target = null;
                return;
            }
            const bounds = target.getBoundingClientRect();
            drag.target = target;
            drag.after = event.clientX > bounds.left + bounds.width / 2;
            target.classList.add(drag.after ? "erp-column-drop-after" : "erp-column-drop-before");
        });
        function finish() {
            if (!drag) return;
            if (drag.active) {
                context.suppressClickUntil = Date.now() + 350;
                if (drag.target) {
                    const sourceKey = drag.header.dataset.erpColumnKey;
                    const targetKey = drag.target.dataset.erpColumnKey;
                    const order = context.order.filter((key) => key !== sourceKey);
                    let index = order.indexOf(targetKey);
                    if (drag.after) index += 1;
                    order.splice(index, 0, sourceKey);
                    context.order = order;
                    applyOrder(context);
                    applyWidths(context);
                    saveState(context);
                    context.table.dispatchEvent(new CustomEvent("erp:columns-changed", {detail: {order: order.slice()}}));
                }
                drag.header.classList.remove("erp-column-dragging");
                drag.preview.remove();
                clearDrop(context);
                document.body.classList.remove("erp-column-table-interacting");
            }
            drag = null;
        }
        root.addEventListener("pointerup", finish);
        root.addEventListener("pointercancel", finish);
        context.table.addEventListener("click", (event) => {
            if (Date.now() < context.suppressClickUntil) {
                event.preventDefault();
                event.stopPropagation();
            }
        }, true);
    }

    function installResize(context) {
        if (!context.resize) return;
        Array.from(context.header.cells).forEach((header) => {
            const handle = document.createElement("span");
            handle.className = "erp-column-resize-handle";
            handle.setAttribute("aria-hidden", "true");
            header.appendChild(handle);
            handle.addEventListener("pointerdown", (event) => {
                event.preventDefault();
                event.stopPropagation();
                const key = header.dataset.erpColumnKey;
                const startX = event.clientX;
                const startWidth = context.widths[key];
                handle.classList.add("is-active");
                document.body.classList.add("erp-column-table-resizing");
                function move(moveEvent) {
                    context.widths[key] = Math.max(MINIMUM_WIDTH, Math.min(MAXIMUM_WIDTH, Math.round(startWidth + moveEvent.clientX - startX)));
                    applyWidths(context);
                }
                function finish() {
                    context.suppressClickUntil = Date.now() + 350;
                    handle.classList.remove("is-active");
                    document.body.classList.remove("erp-column-table-resizing");
                    saveState(context);
                    root.removeEventListener("pointermove", move);
                    root.removeEventListener("pointerup", finish);
                    root.removeEventListener("pointercancel", finish);
                }
                root.addEventListener("pointermove", move);
                root.addEventListener("pointerup", finish, {once: true});
                root.addEventListener("pointercancel", finish, {once: true});
            });
        });
    }

    function initialize(table) {
        if (managed.has(table) || table.dataset.erpColumnControls === "off" || table.closest("[data-erp-column-controls='off']")) return;
        if (root.matchMedia && root.matchMedia("(max-width: 767px)").matches) return;
        if (table.classList.contains("orders-split-table")) return;
        if (!table.isConnected || table.hidden || table.getClientRects().length === 0) return;
        const header = headerRow(table);
        if (!header) {
            table.dataset.erpColumnControls = "unsupported";
            return;
        }
        if (isNativeManaged(table)) {
            table.dataset.erpColumnControls = "native";
            managed.set(table, {native: true});
            return;
        }
        const headers = Array.from(header.cells);
        const keys = buildKeys(headers);
        headers.forEach((cell, index) => { cell.dataset.erpColumnKey = keys[index]; });
        tagCells(table, keys);
        const nativeResize = Boolean(header.querySelector(".receipt-column-resize-handle,.warehouse-column-resize-handle,.sales-column-resize-handle"));
        const colgroup = ensureColgroup(table, keys);
        const storageKey = tableIdentity(table, keys);
        const state = readState(storageKey, keys);
        const context = {
            table, header, keys, colgroup, storageKey,
            order: state.order,
            widths: state.widths,
            resize: !nativeResize,
            scroll: ensureScrollContainer(table),
            suppressClickUntil: 0,
        };
        managed.set(table, context);
        table.dataset.erpColumnControls = "managed";
        applyOrder(context);
        if (context.resize) {
            context.order.forEach((key) => {
                if (context.widths[key]) return;
                const cell = header.querySelector('[data-erp-column-key="' + CSS.escape(key) + '"]');
                context.widths[key] = Math.max(MINIMUM_WIDTH, Math.round(cell.getBoundingClientRect().width || 120));
            });
            applyWidths(context);
        }
        installDrag(context);
        installResize(context);
        saveState(context);
    }

    function refresh() {
        scanQueued = false;
        document.querySelectorAll("table").forEach((table) => {
            const context = managed.get(table);
            if (!context || context.native) {
                initialize(table);
                return;
            }
            if (!context.header.isConnected || headerRow(table) !== context.header) {
                managed.delete(table);
                delete table.dataset.erpColumnControls;
                initialize(table);
                return;
            }
            if (table.classList.contains("orders-split-table")) {
                applyOrder(context, context.keys);
                table.dataset.erpColumnsSuspended = "true";
                return;
            }
            delete table.dataset.erpColumnsSuspended;
            tagCells(table, context.keys);
            applyOrder(context);
            applyWidths(context);
        });
    }

    function queueRefresh() {
        if (scanQueued) return;
        scanQueued = true;
        root.requestAnimationFrame(refresh);
    }

    function start() {
        refresh();
        const observer = new MutationObserver(queueRefresh);
        observer.observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ["class", "hidden"]});
        root.addEventListener("pageshow", queueRefresh);
        root.addEventListener("resize", queueRefresh, {passive: true});
        root.addEventListener("pagehide", (event) => {
            if (!event.persisted) observer.disconnect();
        }, {once: true});
    }

    root.ErpTableColumns = {initialize, refresh, normalizeLabel};
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
    else start();
})(window);
