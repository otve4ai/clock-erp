(function (global) {
    "use strict";

    const AUTO_CLOSE_MS = 9000;

    function stockText(value) {
        const number = Number(value);
        return Number.isFinite(number)
            ? (Number.isInteger(number) ? String(number) : String(Number(number.toFixed(3))))
            : "—";
    }

    function image(item) {
        if (!item.image_url) {
            const placeholder = document.createElement("span");
            placeholder.className = "sale-stock-toast__photo-placeholder";
            placeholder.setAttribute("aria-hidden", "true");
            placeholder.textContent = "□";
            return placeholder;
        }
        const node = document.createElement("img");
        node.className = "sale-stock-toast__photo";
        node.src = item.image_url;
        node.alt = "";
        node.loading = "lazy";
        return node;
    }

    function show(payload) {
        if (!payload || !Array.isArray(payload.items) || !payload.items.length) return null;
        let region = document.querySelector(".sale-stock-toast-region");
        if (!region) {
            region = document.createElement("section");
            region.className = "erp-toast-region sale-stock-toast-region";
            region.setAttribute("aria-label", "Системные уведомления");
            document.body.appendChild(region);
        }

        const card = document.createElement("article");
        card.className = "sale-stock-toast";
        card.dataset.saleStockNotification = "";
        card.setAttribute("role", "status");
        card.setAttribute("aria-live", "polite");

        const icon = document.createElement("span");
        icon.className = "sale-stock-toast__icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = "✓";

        const content = document.createElement("div");
        content.className = "sale-stock-toast__content";
        const title = document.createElement("h2");
        title.className = "sale-stock-toast__title";
        title.textContent = "Продажа проведена";
        const order = document.createElement("p");
        order.className = "sale-stock-toast__order";
        order.textContent = "Заказ №" + String(payload.order_number || "—");
        const items = document.createElement("div");
        items.className = "sale-stock-toast__items";

        payload.items.forEach(function (item) {
            const empty = Number(item.stock_after) === 0;
            const row = document.createElement("div");
            row.className = "sale-stock-toast__item" + (empty ? " is-empty" : "");
            row.dataset.productId = String(item.product_id || "");
            const product = document.createElement("div");
            product.className = "sale-stock-toast__product";
            const name = document.createElement("div");
            name.className = "sale-stock-toast__name";
            name.textContent = String(item.name || "Товар");
            const label = document.createElement("div");
            label.className = "sale-stock-toast__label";
            label.textContent = "Остаток на складе:";
            const change = document.createElement("div");
            change.className = "sale-stock-toast__change";
            const before = document.createElement("span");
            before.textContent = stockText(item.stock_before);
            const arrow = document.createElement("span");
            arrow.setAttribute("aria-hidden", "true");
            arrow.textContent = "→";
            const after = document.createElement("span");
            after.className = "sale-stock-toast__after";
            after.textContent = stockText(item.stock_after);
            change.append(before, arrow, after);
            if (empty) {
                const warning = document.createElement("span");
                warning.className = "sale-stock-toast__warning";
                warning.textContent = "ТОВАР ЗАКОНЧИЛСЯ";
                change.appendChild(warning);
            }
            product.append(name, label, change);
            row.append(image(item), product);
            items.appendChild(row);
        });

        content.append(title, order, items);
        const close = document.createElement("button");
        close.type = "button";
        close.className = "sale-stock-toast__close";
        close.setAttribute("aria-label", "Закрыть уведомление");
        close.textContent = "×";
        card.append(icon, content, close);
        region.appendChild(card);

        let remaining = AUTO_CLOSE_MS;
        let startedAt = Date.now();
        let timer = null;
        const dismiss = function () {
            if (timer) global.clearTimeout(timer);
            timer = null;
            card.remove();
        };
        const resume = function () {
            if (!card.isConnected || timer || remaining <= 0) return;
            startedAt = Date.now();
            timer = global.setTimeout(dismiss, remaining);
        };
        const pause = function () {
            if (!timer) return;
            global.clearTimeout(timer);
            timer = null;
            remaining = Math.max(0, remaining - (Date.now() - startedAt));
        };
        close.addEventListener("click", dismiss);
        card.addEventListener("mouseenter", pause);
        card.addEventListener("mouseleave", resume);
        card.addEventListener("focusin", pause);
        card.addEventListener("focusout", function (event) {
            if (!card.contains(event.relatedTarget)) resume();
        });
        resume();
        return card;
    }

    global.VechasuSaleStockNotification = Object.freeze({show: show});
    document.addEventListener("DOMContentLoaded", function () {
        const source = document.querySelector("[data-sale-stock-notification]");
        if (!source) return;
        try { show(JSON.parse(source.textContent || "null")); } catch (_error) { /* no success card for invalid data */ }
        const url = new URL(global.location.href);
        if (url.searchParams.has("stock_notice")) {
            url.searchParams.delete("stock_notice");
            global.history.replaceState(global.history.state, "", url.pathname + url.search + url.hash);
        }
    });
})(window);
