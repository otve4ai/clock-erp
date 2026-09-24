(() => {
    const box = document.querySelector('[data-required-strap-settings]');
    if (!box) return;
    const checkbox = box.querySelector('[data-requires-strap]');
    const status = box.querySelector('[data-required-strap-status]');
    let productId = null;
    let generation = 0;
    let canManage = false;
    const setStatus = (message, isError = false) => {
        status.textContent = message;
        status.classList.toggle('is-error', isError);
    };
    window.loadRequiredStrapSetting = async id => {
        productId = id;
        const current = ++generation;
        canManage = false;
        checkbox.disabled = true;
        checkbox.checked = false;
        setStatus('');
        try {
            const response = await fetch(`/api/v1/products/${id}/required-strap`);
            const payload = await response.json();
            if (!response.ok) throw new Error('Не удалось загрузить настройку');
            if (current !== generation) return;
            checkbox.checked = payload.data.requires_strap;
            canManage = payload.data.can_manage;
            checkbox.disabled = !canManage;
            setStatus(canManage ? '' : 'Настройку меняет администратор.');
        } catch (error) {
            if (current === generation) setStatus(error.message, true);
        }
    };
    checkbox.addEventListener('change', async () => {
        const current = generation;
        const enabled = checkbox.checked;
        checkbox.disabled = true;
        setStatus('Сохраняем…');
        try {
            const response = await fetch(`/api/v1/products/${productId}/required-strap`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('#inlineProductForm [name="csrf_token"]').value},
                body: JSON.stringify({requires_strap: enabled}),
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error?.message || payload.message || 'Не удалось сохранить настройку');
            if (current === generation) setStatus('Сохранено сразу.');
        } catch (error) {
            if (current === generation) {
                checkbox.checked = !enabled;
                setStatus(error.message, true);
            }
        } finally {
            if (current === generation) checkbox.disabled = !canManage;
        }
    });
})();
