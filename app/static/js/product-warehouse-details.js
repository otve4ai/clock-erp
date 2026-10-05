/* Product metadata is shared; quantities and movement deltas belong to a warehouse. */
(() => {
    const pending = new WeakMap();

    function textElement(tag, text, className = '') {
        const element = document.createElement(tag);
        element.textContent = String(text);
        element.className = className;
        return element;
    }

    function quantityText(stock) {
        return stock.confirmed === false || stock.quantity == null
            ? 'Не подтверждён' : `${stock.quantity} шт.`;
    }

    function stockRow(stock, selectedWarehouse, primary = false) {
        const selected = stock.id === selectedWarehouse;
        const row = textElement('div', '', 'product-warehouse-stock' + (selected ? ' is-selected' : ''));
        const name = stock.name + (stock.active === false ? ' · отключён' : '');
        row.append(textElement('span', name, 'product-warehouse-stock__name'));
        row.append(textElement('strong', quantityText(stock), 'product-warehouse-stock__quantity'));
        if (selected && !primary) {
            row.append(textElement('small', 'Выбранный склад', 'product-warehouse-stock__note'));
        }
        if (Number(stock.in_transit) > 0) {
            row.append(textElement('small', `В пути на склад: ${stock.in_transit} шт.`, 'product-warehouse-stock__transit'));
        }
        return row;
    }

    function renderStocks(container, stocks, selectedWarehouse) {
        container.replaceChildren();
        const primary = stocks.find(stock => stock.id === 'default');
        if (primary) container.append(stockRow(primary, selectedWarehouse, true));
        const others = stocks.filter(stock => stock.id !== 'default' && (
            Number(stock.quantity) > 0 || Number(stock.in_transit) > 0
            || stock.confirmed === false || stock.quantity == null || stock.id === selectedWarehouse
        ));
        if (others.length) {
            const selected = others.find(stock => stock.id === selectedWarehouse);
            const details = textElement('details', '', 'product-warehouse-others');
            let caption = `Другие склады · ${others.length}`;
            if (selected) {
                caption = `${selected.name}: ${quantityText(selected)}`;
                if (others.length > 1) caption += ` · ещё ${others.length - 1}`;
                if (Number(selected.in_transit) > 0) caption += ` · в пути: ${selected.in_transit}`;
            } else {
                const inTransit = others.reduce((sum, stock) => sum + Number(stock.in_transit || 0), 0);
                if (inTransit > 0) caption += ` · в пути: ${inTransit}`;
            }
            const summary = textElement('summary', caption, 'product-warehouse-others__summary');
            summary.title = selected ? 'Выбранный склад. Раскрыть остальные остатки' : 'Склады с остатком или товаром в пути';
            const list = textElement('div', '', 'product-warehouse-others__list');
            list.setAttribute('role', 'region');
            list.setAttribute('aria-label', 'Остатки на других складах');
            list.tabIndex = 0;
            others.forEach(stock => list.append(stockRow(stock, selectedWarehouse)));
            details.append(summary, list);
            container.append(details);
        }
        if (!primary && !others.length) container.textContent = 'Нет данных по складам.';
    }

    function renderHistory(container, operations) {
        container.replaceChildren();
        if (!operations.length) container.textContent = 'Истории по этой позиции пока нет.';
        operations.forEach(operation => {
            const row = textElement('div', '', 'product-stock-history-entry');
            const isPhoto = operation.type === 'product_photo';
            const difference = Number(operation.diff || 0);
            const label = operation.label || 'Операция';
            row.append(textElement('strong', isPhoto ? label : `${label}: ${difference > 0 ? '+' : ''}${difference} шт.`));
            if (!isPhoto) {
                const warehouse = operation.warehouse_name || operation.warehouse_id || 'Склад не указан';
                row.append(textElement('span', `Склад: ${warehouse} · ${operation.stock_before ?? '—'} → ${operation.stock_after ?? '—'} шт.`));
            }
            row.append(textElement('span', operation.created_at || ''));
            const fields = [
                ['Продажа №', operation.sale_id],
                ['Документ: ', operation.document_number || operation.receipt_number],
                ['Источник: ', operation.source],
                ['Пользователь: ', operation.user_name],
                [isPhoto ? '' : 'Причина: ', operation.reason],
                ['МойСклад: ', operation.moysklad_document_name],
            ];
            fields.forEach(([prefix, value]) => {
                if (value) row.append(textElement('span', prefix + value));
            });
            container.append(row);
        });
    }

    async function load(container, productId, resource, render, loading, failure) {
        if (!container) return;
        const token = {};
        pending.set(container, token);
        container.textContent = loading;
        container.setAttribute('aria-busy', 'true');
        try {
            const response = await fetch(`/api/v1/products/${encodeURIComponent(productId)}/${resource}`, {
                cache: 'no-store', headers: {Accept: 'application/json'},
            });
            const payload = await response.json();
            if (!response.ok || !Array.isArray(payload.data)) throw new Error('unavailable');
            if (pending.get(container) === token) render(container, payload.data);
        } catch (error) {
            if (pending.get(container) === token) container.textContent = failure;
        } finally {
            if (pending.get(container) === token) container.setAttribute('aria-busy', 'false');
        }
    }

    window.ProductWarehouseDetails = {
        loadStocks(container, productId, selectedWarehouse) {
            return load(container, productId, 'warehouse-stocks',
                (element, data) => renderStocks(element, data, selectedWarehouse),
                'Загружаем остатки…', 'Не удалось загрузить остатки. Откройте карточку ещё раз.');
        },
        loadHistory(container, productId) {
            return load(container, productId, 'movements?limit=10', renderHistory,
                'Загружаем историю…', 'Не удалось загрузить историю. Откройте карточку ещё раз.');
        },
    };
})();
