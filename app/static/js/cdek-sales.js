(() => {
    const drawer = document.querySelector('.cdek-drawer');
    if (!drawer || typeof drawer.showModal !== 'function') return;
    drawer.removeAttribute('open');
    drawer.showModal();
    document.body.classList.add('cdek-drawer-visible');
    const close = () => drawer.close();
    drawer.querySelectorAll('[data-cdek-close]').forEach(link => {
        link.addEventListener('click', event => { event.preventDefault(); close(); });
    });
    drawer.addEventListener('cancel', event => { event.preventDefault(); close(); });
    drawer.addEventListener('close', () => {
        document.body.classList.remove('cdek-drawer-visible');
        const url = new URL(window.location.href);
        url.searchParams.delete('shipment');
        url.searchParams.delete('message');
        url.hash = '';
        history.replaceState(null, '', url);
        const row = document.getElementById('shipment-' + drawer.dataset.shipment);
        if (row) row.querySelectorAll('[aria-expanded]').forEach(link => link.setAttribute('aria-expanded', 'false'));
        const opener = row && row.querySelector('.cdek-open');
        if (opener) opener.focus({preventScroll: true});
    });
})();
