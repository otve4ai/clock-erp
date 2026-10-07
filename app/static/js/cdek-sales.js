(() => {
    document.querySelectorAll('[data-transient-notice]').forEach(node => setTimeout(() => node.remove(), 4500));
    let tooltip;
    const hideTooltip = () => { if (tooltip) tooltip.remove(); tooltip = null; };
    document.querySelectorAll('[data-contact-detail]').forEach(button => {
        const show = () => {
            hideTooltip();
            tooltip = document.createElement('div');
            tooltip.className = 'cdek-contact-popover';
            tooltip.setAttribute('role', 'status');
            tooltip.textContent = button.dataset.contactDetail;
            document.body.append(tooltip);
            const rect = button.getBoundingClientRect();
            tooltip.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - tooltip.offsetWidth - 8)) + 'px';
            tooltip.style.top = Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - tooltip.offsetHeight - 8)) + 'px';
        };
        button.addEventListener('click', show);
        button.addEventListener('focus', show);
        button.addEventListener('mouseenter', show);
        button.addEventListener('mouseleave', hideTooltip);
        button.addEventListener('blur', hideTooltip);
    });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTooltip(); });
    window.addEventListener('scroll', hideTooltip, true);
    const refreshContacts = async () => {
        if (document.hidden) return;
        const rows = [...document.querySelectorAll('tr[id^="shipment-"]')];
        const detail = document.querySelector('.cdek-drawer');
        const keys = new Set(rows.map(row => row.id.slice(9)));
        if (detail) keys.add(detail.dataset.shipment);
        if (!keys.size) return;
        const url = new URL('contacts', window.location.origin + window.location.pathname.replace(/\/$/, '') + '/');
        keys.forEach(key => url.searchParams.append('id', key));
        try {
            const response = await fetch(url, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
            if (!response.ok) return;
            const data = await response.json();
            rows.forEach(row => row.querySelectorAll('[data-contact-channel]').forEach(button => {
                const value = data[row.id.slice(9)]?.[button.dataset.contactChannel];
                if (!value) return;
                button.className = 'cdek-contact cdek-badge cdek-' + value.tone;
                button.dataset.contactDetail = button.dataset.contactLabel + ': ' + value.tooltip;
                button.title = button.dataset.contactDetail;
                button.setAttribute('aria-label', button.dataset.contactDetail);
                button.querySelector('span').textContent = value.mark;
            }));
            detail?.querySelectorAll('[data-contact-summary]').forEach(line => {
                const value = data[detail.dataset.shipment]?.[line.dataset.contactSummary];
                if (!value) return;
                const badge = line.querySelector('.cdek-badge');
                badge.className = 'cdek-badge cdek-' + value.tone;
                badge.textContent = value.label;
                line.querySelector('small').textContent = value.date ? value.date + ' МСК' : 'Дата неизвестна';
                line.title = value.tooltip;
            });
        } catch (_) { /* Keep the last confirmed journal state on transport failure. */ }
    };
    setInterval(refreshContacts, 30000);
    const form = document.getElementById('cdek-manager-form');
    if (form) {
        const updateFields = () => {
            const work = form.elements.review_work.value;
            form.querySelectorAll('[data-work-fields]').forEach(node => { node.hidden = node.dataset.workFields !== work; });
            form.elements.outcome.required = work === 'closed';
            const other = work === 'closed' && form.elements.outcome.value === 'other';
            form.querySelector('[data-outcome-note]').hidden = !other;
            form.elements.outcome_note.required = other;
            form.elements.call_at.required = !!form.elements.call_result.value;
        };
        form.addEventListener('change', updateFields);
        updateFields();
    }
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
