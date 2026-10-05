(() => {
    const warehouse = new URL(location.href).searchParams.get('warehouse_id') || 'default';
    document.getElementById('warehouseSelector')?.addEventListener('change', event => {
        const url = new window.URL(window.location.href);
        url.searchParams.set('warehouse_id', event.currentTarget.value);
        url.searchParams.delete('page');
        window.location.assign(url.href);
    });
    function preserveWarehouse(form) {
        if (!form.querySelector('[name="warehouse_id"]')) {
            const field = document.createElement('input');
            field.type = 'hidden'; field.name = 'warehouse_id'; field.value = warehouse;
            form.append(field);
        }
    }
    document.querySelectorAll('form[method="get"]').forEach(preserveWarehouse);
    // Pagination is replaced by partial navigation; add the scope before its
    // existing submit handler constructs FormData, including replacement forms.
    document.addEventListener('submit', event => {
        if (event.target.matches('form[method="get"]')) preserveWarehouse(event.target);
    }, true);
    document.addEventListener('click', event => {
        const marker = event.target.closest('.other-warehouse-dot');
        document.querySelectorAll('.other-warehouse-dot').forEach(button => {
            if (button !== marker) button.nextElementSibling.hidden = true;
            button.setAttribute('aria-expanded', String(!button.nextElementSibling.hidden));
        });
    });
    document.addEventListener('click', event => {
        const link = event.target.closest('a[href]');
        if (!link) return;
        const url = new URL(link.href, location.href);
        if (url.origin === location.origin && ['/warehouse', '/app/products'].includes(url.pathname)) {
            url.searchParams.set('warehouse_id', warehouse); link.href = url.href;
        }
    }, true);
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            document.querySelectorAll('.warehouse-dot-detail').forEach(el => el.hidden = true);
            document.querySelectorAll('.other-warehouse-dot').forEach(el => el.setAttribute('aria-expanded', 'false'));
        }
    });
})();
