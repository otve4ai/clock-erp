(() => {
    const box = document.querySelector('[data-required-strap-settings]');
    if (!box) return;
    const checkbox = box.querySelector('[data-requires-strap]');
    const status = box.querySelector('[data-required-strap-status]');
    let productId = null;
    let generation = 0;
    window.loadRequiredStrapSetting = async id => {
        productId = id;
        const current = ++generation;
        checkbox.disabled = true;
        checkbox.checked = false;
        status.textContent = 'Загрузка настройки…';
        try {
            const response = await fetch(`/api/v1/products/${id}/required-strap`);
            const payload = await response.json();
            if (!response.ok) throw new Error('Не удалось загрузить настройку');
            if (current !== generation) return;
            checkbox.checked = payload.data.requires_strap;
            checkbox.disabled = !payload.data.can_manage;
            status.textContent = payload.data.can_manage
                ? 'Для отмеченного товара при продаже из заказа обязателен ремешок. Настройка сохраняется сразу.'
                : 'Настройку меняет администратор.';
        } catch (error) {
            if (current === generation) status.textContent = error.message;
        }
    };
    checkbox.addEventListener('change', async () => {
        const current = generation;
        const enabled = checkbox.checked;
        checkbox.disabled = true;
        status.textContent = 'Сохраняем…';
        try {
            const response = await fetch(`/api/v1/products/${productId}/required-strap`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('#inlineProductForm [name="csrf_token"]').value},
                body: JSON.stringify({requires_strap: enabled}),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error?.message || payload.message || 'Не удалось сохранить настройку');
            if (current === generation) status.textContent = 'Настройка сохранена.';
        } catch (error) {
            if (current === generation) {
                checkbox.checked = !enabled;
                status.textContent = error.message;
            }
        } finally {
            if (current === generation) checkbox.disabled = false;
        }
    });
})();
