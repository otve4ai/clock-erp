/* Product metadata is shared; quantities and movement deltas belong to a warehouse. */
(() => {
    const pending = new WeakMap();
    const selections = new WeakMap();
    let pickerSequence = 0;
    let openPicker = null;

    document.addEventListener('pointerdown', event => {
        if (openPicker && !openPicker.root.contains(event.target)) openPicker.close();
    });
    document.addEventListener('focusin', event => {
        if (openPicker && !openPicker.root.contains(event.target)) openPicker.close();
    });

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

    function warehouseName(stock) {
        return stock.name + (stock.active === false ? ' · отключён' : '');
    }

    function stockRow(stock) {
        const row = textElement('button', '', 'product-warehouse-option');
        row.type = 'button';
        row.tabIndex = -1;
        row.setAttribute('role', 'option');
        const check = textElement('span', '✓', 'product-warehouse-option__check');
        check.setAttribute('aria-hidden', 'true');
        row.append(check, textElement('span', warehouseName(stock), 'product-warehouse-option__name'));
        row.append(textElement('strong', quantityText(stock), 'product-warehouse-option__quantity'));
        if (Number(stock.in_transit) > 0) {
            row.append(textElement('small', `В пути на склад: ${stock.in_transit} шт.`, 'product-warehouse-option__transit'));
        }
        return row;
    }

    function renderStocks(container, stocks, selectedWarehouse, selection, editor) {
        container.replaceChildren();
        // TTT remains first; even a zero balance must be available in the picker.
        const ordered = stocks.filter(stock => stock.id === 'default')
            .concat(stocks.filter(stock => stock.id !== 'default'));
        if (!ordered.length) {
            container.textContent = 'Нет данных по складам.';
            return;
        }
        let selectedIndex = ordered.findIndex(stock => stock.id === selectedWarehouse);
        if (selectedIndex < 0) selectedIndex = 0;
        const root = textElement('div', '', 'product-warehouse-picker');
        const trigger = textElement('button', '', 'product-warehouse-picker__trigger');
        trigger.type = 'button';
        trigger.setAttribute('aria-haspopup', 'listbox');
        trigger.setAttribute('aria-expanded', 'false');
        const caption = textElement('span', '', 'product-warehouse-picker__caption');
        const arrow = textElement('span', '', 'product-warehouse-picker__arrow');
        arrow.setAttribute('aria-hidden', 'true');
        trigger.append(caption, arrow);
        const amount = textElement('strong', '', 'product-warehouse-picker__quantity');
        const transit = textElement('span', '', 'product-warehouse-picker__transit');
        const list = textElement('div', '', 'product-warehouse-picker__list');
        list.id = `product-warehouse-options-${++pickerSequence}`;
        list.hidden = true;
        list.setAttribute('role', 'listbox');
        list.setAttribute('aria-label', 'Склады и остатки');
        trigger.setAttribute('aria-controls', list.id);
        const options = ordered.map(stockRow);
        list.append(...options);
        root.append(trigger, amount, transit, list);
        container.append(root);

        function update() {
            const stock = ordered[selectedIndex];
            caption.textContent = warehouseName(stock);
            trigger.title = `${warehouseName(stock)}: ${quantityText(stock)}`;
            trigger.setAttribute('aria-label', `Склад: ${warehouseName(stock)}. Остаток: ${quantityText(stock)}`);
            amount.textContent = quantityText(stock);
            amount.title = trigger.title;
            transit.textContent = Number(stock.in_transit) > 0 ? `В пути: ${stock.in_transit} шт.` : '';
            options.forEach((option, index) => option.setAttribute('aria-selected', String(index === selectedIndex)));
            selection.warehouseId = stock.id;
            if (editor) {
                const {input, label} = editor;
                label.textContent = `Остаток ${stock.name}`;
                input.setAttribute('aria-label', label.textContent);
                input.value = stock.quantity == null ? '' : String(stock.quantity);
                input.dataset.originalValue = input.value;
                input.dataset.warehouseId = stock.id;
                input.disabled = stock.active === false || stock.editable === false
                    || stock.confirmed === false || stock.quantity == null;
                input.title = input.disabled ? 'Корректировка недоступна. Используйте приход или инвентаризацию.' : '';
            }
        }
        function close(restoreFocus = false) {
            list.hidden = true;
            trigger.setAttribute('aria-expanded', 'false');
            if (openPicker?.root === root) openPicker = null;
            if (restoreFocus) trigger.focus();
        }
        function focusOption(index) {
            const option = options[index];
            option.focus({preventScroll: true});
            // Scroll only the popup, never the product drawer behind it.
            if (option.offsetTop < list.scrollTop) list.scrollTop = option.offsetTop;
            else if (option.offsetTop + option.offsetHeight > list.scrollTop + list.clientHeight) {
                list.scrollTop = option.offsetTop + option.offsetHeight - list.clientHeight;
            }
        }
        function open() {
            if (openPicker) openPicker.close();
            const bounds = root.getBoundingClientRect();
            const below = window.innerHeight - bounds.bottom - 16;
            const above = bounds.top - 80;
            const upwards = below < 200 && above > below;
            list.dataset.direction = upwards ? 'up' : 'down';
            list.style.maxHeight = `${Math.max(60, Math.min(240, upwards ? above : below))}px`;
            list.hidden = false;
            trigger.setAttribute('aria-expanded', 'true');
            openPicker = {root, container, close};
            focusOption(selectedIndex);
        }
        trigger.addEventListener('click', () => list.hidden ? open() : close(true));
        options.forEach((option, index) => option.addEventListener('click', () => {
            if (index === selectedIndex) { close(true); return; }
            if (editor && index !== selectedIndex && (
                editor.isSaving?.() || (!editor.input.disabled
                    && editor.input.value !== editor.input.dataset.originalValue)
            )) {
                close(true);
                editor.onBlocked?.();
                return;
            }
            selectedIndex = index;
            update();
            close(true);
        }));
        root.addEventListener('keydown', event => {
            if (event.key === 'Escape' && !list.hidden) {
                event.preventDefault();
                event.stopPropagation();
                close(true);
            } else if (event.key === 'Tab' && !list.hidden) {
                // Let native Tab continue from the trigger, not a hidden option.
                close(true);
            } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
                event.preventDefault();
                if (list.hidden) { open(); return; }
                let index = options.indexOf(document.activeElement);
                if (event.key === 'Home') index = 0;
                else if (event.key === 'End') index = options.length - 1;
                else index = (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
                focusOption(index);
            }
        });
        update();
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
        loadStocks(container, productId, selectedWarehouse, editor) {
            if (!container) return;
            if (openPicker?.container === container) openPicker.close();
            if (editor) {
                editor.input.disabled = true;
                editor.input.value = '';
                editor.input.dataset.originalValue = '';
                editor.input.dataset.warehouseId = '';
                editor.label.textContent = 'Остаток выбранного склада';
            }
            let selection = selections.get(container);
            if (!selection || selection.productId !== String(productId) || selection.context !== selectedWarehouse) {
                selection = {productId: String(productId), context: selectedWarehouse, warehouseId: selectedWarehouse};
                selections.set(container, selection);
            }
            return load(container, productId, 'warehouse-stocks',
                (element, data) => renderStocks(element, data, selection.warehouseId, selection, editor),
                'Загружаем остатки…', 'Не удалось загрузить остатки. Откройте карточку ещё раз.');
        },
        appendStockChange(body, input) {
            ['stock', 'stock_reason', 'stock_warehouse_id', 'stock_expected'].forEach(key => body.delete(key));
            if (input.disabled || input.readOnly || !input.name || input.value === input.dataset.originalValue) return;
            if (!input.dataset.warehouseId || input.value.trim() === ''
                || !Number.isSafeInteger(Number(input.value)) || Number(input.value) < 0) {
                throw new Error('Укажите целый неотрицательный остаток выбранного склада.');
            }
            body.set('stock', input.value);
            body.set('stock_warehouse_id', input.dataset.warehouseId);
            body.set('stock_expected', input.dataset.originalValue);
            body.set('stock_reason', 'Редактирование карточки товара');
        },
        loadHistory(container, productId) {
            return load(container, productId, 'movements?limit=10', renderHistory,
                'Загружаем историю…', 'Не удалось загрузить историю. Откройте карточку ещё раз.');
        },
    };
})();
