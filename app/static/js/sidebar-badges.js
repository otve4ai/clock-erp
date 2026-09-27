(() => {
    "use strict";
    if (window.__erpSidebarBadgesInitialized) return;
    window.__erpSidebarBadgesInitialized = true;

    const labels = {
        inbox: "Непрочитанных входящих",
    };
    const refresh = async (kind, nodes) => {
        if (!Object.prototype.hasOwnProperty.call(labels, kind)) return;
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), 1500);
        try {
            const response = await fetch(`/api/v1/${kind}/badge`, {
                credentials: "same-origin", signal: controller.signal,
                headers: {Accept: "application/json", "X-Vechasu-Notify": "off"},
            });
            if (!response.ok) return;
            const payload = await response.json();
            const count = payload.data && payload.data.count;
            if (!Number.isSafeInteger(count) || count < 0) return;
            nodes.forEach((node) => {
                node.textContent = count > 999 ? "999+" : String(count);
                node.setAttribute("aria-label", `${labels[kind]}: ${count}`);
                node.hidden = count === 0;
            });
        } catch (_) {
            // An unavailable optional counter must not affect ERP navigation.
        } finally {
            window.clearTimeout(timer);
        }
    };
    const start = () => {
        const groups = new Map();
        document.querySelectorAll("[data-sidebar-badge]").forEach((node) => {
            const kind = node.dataset.sidebarBadge;
            if (!groups.has(kind)) groups.set(kind, []);
            groups.get(kind).push(node);
        });
        groups.forEach((nodes, kind) => { refresh(kind, nodes).catch(() => {}); });
    };
    if (document.readyState === "complete") window.setTimeout(start, 0);
    else window.addEventListener("load", start, {once: true});
})();
