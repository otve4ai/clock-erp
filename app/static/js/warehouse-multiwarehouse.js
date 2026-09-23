(() => {
  const csrf = window.__warehouseCsrfToken || "";
  const json = async (url, options = {}) => {
    const response = await fetch(url, {headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, ...options});
    const payload = await response.json();
    if (!response.ok || payload.ok === false) throw new Error(payload.message || "Не удалось выполнить операцию.");
    return payload;
  };
  const errorBox = document.getElementById("warehouseSelectorError");
  document.addEventListener("change", async event => {
    if (!event.target.matches("[data-warehouse-choice]")) return;
    const choices = [...document.querySelectorAll("[data-warehouse-choice]")];
    const selected = choices.filter(item => item.checked).map(item => Number(item.value));
    if (!selected.length) { event.target.checked = true; errorBox.textContent = "Выберите хотя бы один склад."; return; }
    try { await json("/api/v1/warehouse-preferences", {method: "PUT", body: JSON.stringify({warehouse_ids: selected})}); location.reload(); }
    catch (error) { event.target.checked = !event.target.checked; errorBox.textContent = error.message; }
  });
  document.getElementById("warehouseManagementAddSubmit")?.addEventListener("click", async event => {
    const form = event.target.closest("#warehouseManagementAddForm");
    const name = form.querySelector("[name=name]").value.trim();
    if (!name) { errorBox.textContent = "Введите название склада."; return; }
    try { await json("/api/v1/warehouses", {method: "POST", body: JSON.stringify({name})}); location.reload(); }
    catch (error) { errorBox.textContent = error.message; }
  });
  document.addEventListener("click", async event => {
    const stockButton = event.target.closest(".warehouse-stock-button");
    if (stockButton) {
      const popover = stockButton._warehousePopover || stockButton.nextElementSibling;
      stockButton._warehousePopover = popover;
      if (popover.parentElement !== document.body) document.body.append(popover);
      document.querySelectorAll(".warehouse-stock-popover:not([hidden])").forEach(item => { if (item !== popover) item.hidden = true; });
      popover.hidden = !popover.hidden;
      if (!popover.hidden) {
        const rect = stockButton.getBoundingClientRect();
        popover.style.position = "fixed";
        popover.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - 232))}px`;
        popover.style.top = `${Math.min(rect.bottom + 6, innerHeight - popover.offsetHeight - 12)}px`;
        popover.style.right = "auto";
      }
      return;
    }
    const rename = event.target.closest("[data-warehouse-rename]");
    if (rename) {
      const name = prompt("Новое название склада", rename.dataset.name);
      if (!name) return;
      try { await json(`/api/v1/warehouses/${rename.dataset.warehouseRename}`, {method: "PATCH", body: JSON.stringify({name})}); location.reload(); }
      catch (error) { errorBox.textContent = error.message; } return;
    }
    const archive = event.target.closest("[data-warehouse-archive]");
    if (archive) {
      if (!confirm("Архивировать пустой склад?")) return;
      try { await json(`/api/v1/warehouses/${archive.dataset.warehouseArchive}/archive`, {method: "POST", body: "{}"}); location.reload(); }
      catch (error) { errorBox.textContent = error.message; } return;
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    document.querySelectorAll(".warehouse-stock-popover:not([hidden])").forEach(item => { item.hidden = true; });
    const selector = document.getElementById("warehouseSelector");
    if (selector?.open) { selector.open = false; selector.querySelector("summary")?.focus(); }
  });
  const dialog = document.getElementById("warehouseTransferDialog");
  const transferForm = document.getElementById("warehouseTransferForm");
  document.getElementById("warehouseTransferOpen")?.addEventListener("click", () => dialog.showModal());
  document.querySelector("[data-transfer-close]")?.addEventListener("click", () => dialog.close());
  let searchTimer;
  transferForm?.elements.product_query.addEventListener("input", event => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const from = transferForm.elements.from_warehouse_id.value;
      const payload = await json(`/api/v1/warehouse-products/search?q=${encodeURIComponent(event.target.value)}&warehouse_id=${from}`);
      const results = document.getElementById("warehouseTransferResults");
      results.innerHTML = "";
      payload.items.filter(item => !item.is_bundle).forEach(item => {
        const button = document.createElement("button"); button.type = "button";
        button.textContent = `${item.name} · ${item.stock}`;
        button.addEventListener("click", () => { transferForm.elements.product_id.value = item.id; transferForm.elements.product_query.value = item.name; results.innerHTML = ""; });
        results.append(button);
      });
    }, 180);
  });
  transferForm?.addEventListener("submit", async event => {
    event.preventDefault(); const data = Object.fromEntries(new FormData(event.target));
    data.idempotency_key = crypto.randomUUID();
    try { await json("/api/v1/warehouse-transfers", {method: "POST", body: JSON.stringify(data)}); location.reload(); }
    catch (error) { event.target.querySelector("[data-transfer-error]").textContent = error.message; }
  });
})();
