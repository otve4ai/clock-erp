(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const number = (n) => (n == null ? "—" : Number(n).toLocaleString("ru-RU"));
  const isAdmin = document.body.dataset.isAdmin === "1";
  const image = (url) =>
    /^https?:\/\//.test(url || "") || (url || "").startsWith("/")
      ? `<img src="${esc(url)}" alt="" loading="lazy">`
      : "—";
  const labels = {
    supply: "Поставка",
    manual_receipt: "Приход",
    manual_receipt_reversal: "Отмена прихода",
    sale_cancellation: "Отмена продажи",
    receipt: "Старый приход",
    legacy: "Архивная запись",
    legacy_excel: "Excel — история",
    return: "Возврат продажи",
    manual_adjustment: "Корректировка",
    initial_stock: "Начальный остаток",
    draft: "Черновик",
    posted: "Проведена",
  };
  let tab = new URLSearchParams(location.search).get("tab") || "all";
  let rows = [],
    filtered = [],
    page = 1,
    current = null,
    items = [],
    busy = false;
  let editorType = "supply",
    warehouses = [];
  const hidden = new Set();
  let sortDirection = "desc";
  const storageKey = () => "erp-supply-columns-" + tab;
  const movementColumns = [
    { key: "date", label: "Дата" },
    { key: "comment", label: "Комментарий" },
    { key: "author", label: "Автор" },
    { key: "time", label: "Время" },
    { key: "type", label: "Тип прихода" },
    { key: "document", label: "Документ" },
    { key: "brand", label: "Бренд" },
    { key: "category", label: "Категория" },
    { key: "photo", label: "Фото" },
    { key: "product", label: "Товар" },
    { key: "article", label: "Артикул" },
    { key: "before", label: "Было", numeric: true },
    { key: "change", label: "Изменение", numeric: true },
    { key: "after", label: "Стало", numeric: true },
    { key: "source", label: "Источник" },
  ];
  const supplyColumns = [
    { key: "date", label: "Дата" },
    { key: "type", label: "Тип" },
    { key: "number", label: "Номер" },
    { key: "title", label: "Название / описание" },
    { key: "warehouse", label: "Склад" },
    { key: "positions", label: "Позиций", numeric: true },
    { key: "quantity", label: "Единиц", numeric: true },
    { key: "status", label: "Статус" },
    { key: "author", label: "Автор" },
    { key: "actions", label: "Действия" },
  ];
  const legacyColumnKeys = {
    all: [
      "date",
      "time",
      "type",
      "document",
      "comment",
      "brand",
      "category",
      "photo",
      "product",
      "article",
      "before",
      "change",
      "after",
      "author",
      "source",
    ],
    cancellations: [
      "date",
      "time",
      "type",
      "document",
      "comment",
      "brand",
      "category",
      "photo",
      "product",
      "article",
      "before",
      "change",
      "after",
      "author",
      "source",
    ],
    supplies: [
      "date",
      "number",
      "title",
      "comment",
      "positions",
      "quantity",
      "status",
      "author",
      "actions",
    ],
  };
  function restoreColumns() {
    hidden.clear();
    try {
      for (const value of JSON.parse(
        localStorage.getItem(storageKey()) || "[]",
      )) {
        const key =
          typeof value === "number" ? legacyColumnKeys[tab]?.[value] : value;
        if (key) hidden.add(key);
      }
    } catch {
      /* Fresh defaults when browser storage is unavailable. */
    }
  }
  restoreColumns();
  async function api(path, options = {}) {
    const headers = {
      "X-CSRF-Token": document.querySelector("meta[name=csrf-token]").content,
      ...options.headers,
    };
    if (options.body && !(options.body instanceof FormData))
      headers["Content-Type"] = "application/json";
    const response = await fetch("/api/v1/receipts/" + path, {
      ...options,
      headers,
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error("Не удалось получить ответ сервера. Обновите страницу.");
    }
    if (!response.ok || data.ok === false) {
      const error = new Error(data.message || "Операция отклонена сервером.");
      error.status = response.status;
      error.data = data.data;
      throw error;
    }
    return data.data;
  }
  function message(text, dialog = false) {
    const el = $(dialog ? "dialog-message" : "message");
    el.textContent = text;
    el.hidden = !text;
  }
  async function action(fn, dialog = false) {
    if (busy) return;
    busy = true;
    document.querySelectorAll("button,input,select,textarea").forEach((b) => {
      b.dataset.wasDisabled = b.disabled;
      b.disabled = true;
    });
    try {
      await fn();
    } catch (e) {
      message(e.message, dialog);
    } finally {
      busy = false;
      document.querySelectorAll("button,input,select,textarea").forEach((b) => {
        b.disabled = b.dataset.wasDisabled === "true";
      });
      renderItems();
      render();
      if ($("add-item-dialog").open) updateAddition();
    }
  }
  function options(id, values) {
    const el = $(id),
      previous = el.value,
      label = el.options[0].textContent;
    el.innerHTML =
      `<option value="">${esc(label)}</option>` +
      [...new Set(values.filter(Boolean))]
        .sort()
        .map((v) => `<option>${esc(v)}</option>`)
        .join("");
    el.value = previous;
  }
  async function load() {
    if (!warehouses.length) {
      warehouses = await api("warehouses");
      const choices = warehouses.map((warehouse) =>
        `<option value="${esc(warehouse.id)}">${esc(warehouse.name)}</option>`,
      ).join("");
      $("receipt-warehouse").innerHTML = choices;
      $("warehouse-filter").innerHTML = '<option value="">Все склады</option>' + choices;
    }
    rows = await api(
      tab === "supplies" ? "supplies" : tab === "receipts" ? "manual" : "documents",
    );
    if (tab === "cancellations")
      rows = rows.filter((r) => r.source_type === "sale_cancellation");
    options(
      "brand",
      rows.flatMap((r) => (r.items ? r.items.map((i) => i.brand) : [r.brand])),
    );
    options(
      "category",
      rows.flatMap((r) =>
        r.items ? r.items.map((i) => i.category) : [r.category],
      ),
    );
    document
      .querySelectorAll("[data-tab]")
      .forEach((b) =>
        b.setAttribute(
          "aria-current",
          b.dataset.tab === tab ? "page" : "false",
        ),
      );
    page = 1;
    render();
  }
  function render() {
    const q = $("query").value.toLocaleLowerCase(),
      from = $("date-from").value,
      to = $("date-to").value;
    filtered = rows.filter((r) => {
      const searchable = [
        r.title,
        r.number,
        r.comment,
        r.name,
        r.article,
        r.brand,
        r.user_name,
        r.created_by,
        r.source_id,
        r.reason_label,
        r.warehouse_name,
        ...(r.items || []).flatMap((item) => [item.name, item.product_name, item.article, item.brand, item.category]),
      ]
        .join(" ")
        .toLocaleLowerCase();
      const timestamp = new Date(r.created_at);
      const date = Number.isNaN(timestamp.valueOf())
        ? (r.created_at || "").slice(0, 10)
        : [
            timestamp.getFullYear(),
            String(timestamp.getMonth() + 1).padStart(2, "0"),
            String(timestamp.getDate()).padStart(2, "0"),
          ].join("-");
      return (
        (!q || searchable.includes(q)) &&
        (!from || date >= from) &&
        (!to || date <= to) &&
        (!$("type").value ||
          (r.source_type || r.status) === $("type").value ||
          r.status === $("type").value) &&
        (!$("brand").value ||
          r.brand === $("brand").value ||
          r.items?.some((i) => i.brand === $("brand").value)) &&
        (!$("category").value ||
          r.category === $("category").value ||
          r.items?.some((i) => i.category === $("category").value)) &&
        (!$("warehouse-filter").value ||
          r.warehouse_id === $("warehouse-filter").value)
      );
    });
    filtered.sort((a, b) =>
      sortDirection === "desc"
        ? b.created_at.localeCompare(a.created_at)
        : a.created_at.localeCompare(b.created_at),
    );
    const supplies = true;
    $("count-label").textContent = supplies
      ? tab === "supplies" ? "Поставок" : tab === "receipts" ? "Приходов" : tab === "all" ? "Всего операций" : "Отмен продаж"
      : tab === "cancellations"
        ? "Отмен"
        : "Записей прихода";
    $("count").textContent = number(
      tab === "cancellations"
        ? new Set(filtered.map((r) => r.source_id)).size
        : filtered.length,
    );
    $("quantity-label").textContent =
      tab === "cancellations" ? "Возвращено единиц" : "Принято единиц";
    $("quantity").textContent = number(
      filtered.reduce(
        (sum, r) =>
          sum +
          (supplies
            ? r.status === "posted"
              ? r.total_quantity
              : 0
            : Number(r.quantity)),
        0,
      ),
    );
    const columns = supplies ? supplyColumns : movementColumns;
    $("records").dataset.layout = supplies ? "supplies" : "movements";
    $("columns").innerHTML = columns
      .map(
        (column) =>
          `<label><input type="checkbox" data-col="${column.key}" ${hidden.has(column.key) ? "" : "checked"}>${column.label}</label>`,
      )
      .join("");
    $("records").querySelector("colgroup").innerHTML = columns
      .map(
        (column) =>
          `<col data-column="${column.key}"${hidden.has(column.key) ? " hidden" : ""}>`,
      )
      .join("");
    const cell = (value, column, tag = "td") => {
      const numeric = column.numeric ? " is-numeric" : "";
      const content =
        tag === "td" && !["photo", "actions", "source"].includes(column.key)
          ? `<div class="record-cell-content">${value}</div>`
          : value;
      return `<${tag} data-column="${column.key}" class="record-column-${column.key}${numeric}"${tag === "th" ? ' scope="col"' + (column.key === "date" ? ' aria-sort="' + (sortDirection === "desc" ? "descending" : "ascending") + '"' : "") : ""}${hidden.has(column.key) ? " hidden" : ""}>${content}</${tag}>`;
    };
    $("records").querySelector("thead").innerHTML =
      "<tr>" +
      columns
        .map((column) =>
          cell(
            column.key === "date"
              ? '<button class="button" data-sort-date>Дата ' +
                  (sortDirection === "desc" ? "↓" : "↑") +
                  "</button>"
              : column.label,
            column,
            "th",
          ),
        )
        .join("") +
      "</tr>";
    const size = Number($("page-size").value),
      pages = Math.max(1, Math.ceil(filtered.length / size));
    page = Math.min(page, pages);
    $("records").querySelector("tbody").innerHTML =
      filtered
        .slice((page - 1) * size, page * size)
        .map((r) => {
          const date = new Date(r.created_at),
            valid = !Number.isNaN(date.valueOf());
          const dateText = valid
            ? date.toLocaleDateString("ru-RU")
            : esc(r.created_at || "—");
          const button = `<button class="button" data-open="${esc(r.id)}">Открыть</button>`;
          const values = supplies
              ? {
                date: dateText,
                type: `<span class="type-badge type-${esc(r.source_type || "legacy")}">${esc(labels[r.source_type] || "Архивная запись")}</span>`,
                comment: esc(r.comment || "—"),
                author: esc(r.created_by || r.user_name || "—"),
                number: esc(r.number || "—"),
                title: esc(r.title || r.reason_label || r.comment || "—"),
                warehouse: esc(r.warehouse_name || "—"),
                positions: number(r.position_count),
                quantity: number(r.total_quantity),
                status: labels[r.status] || esc(r.status || "—"),
                actions: button,
              }
            : {
                date: dateText,
                comment: esc(r.comment || "—"),
                author: esc(r.user_name || "—"),
                time: valid ? date.toLocaleTimeString("ru-RU") : "—",
                type: labels[r.source_type] || "Архивная запись",
                document: esc(r.title || "—"),
                brand: esc(r.brand || "—"),
                category: esc(r.category || "—"),
                photo: image(r.image_url),
                product: `<div class="name">${esc(r.name || "—")}</div>`,
                article: esc(r.article || "—"),
                before: number(r.stock_before),
                change: `<span class="positive">+${number(r.quantity)}</span>`,
                after: number(r.stock_after),
                source: button,
              };
          return (
            "<tr>" +
            columns.map((column) => cell(values[column.key], column)).join("") +
            "</tr>"
          );
        })
        .join("") ||
      `<tr><td class="records-empty" colspan="${columns.length}">Записей пока нет</td></tr>`;
    $("page-info").textContent =
      `Страница ${page} из ${pages} · ${filtered.length} записей`;
    $("previous").disabled = page <= 1;
    $("next").disabled = page >= pages;
  }
  async function openSupply(id) {
    editorType = "supply";
    current = id ? await api("supplies/" + encodeURIComponent(id)) : null;
    items = current ? current.items.map((i) => ({ ...i })) : [];
    $("title").value = current?.title || "";
    $("comment").value = current?.comment || "";
    $("receipt-warehouse").value = current?.warehouse_id || warehouses.find((row) => row.is_default)?.id || "";
    $("supply-heading").textContent = current
      ? `Поставка ${current.number}`
      : "Новая поставка";
    $("supply-meta").textContent = current
      ? `${labels[current.status]} · Создана ${current.created_at} · ${current.created_by}${current.posted_at ? " · Проведена " + current.posted_at + " · " + current.posted_by : ""}`
      : "";

    message("", true);
    renderItems();
    if (!$("supply-dialog").open) $("supply-dialog").showModal();
  }
  async function openManualReceipt(id) {
    editorType = "manual_receipt";
    current = id ? await api("manual/" + encodeURIComponent(id)) : null;
    items = current ? current.items.map((item) => ({ ...item })) : [];
    $("title").value = "";
    $("comment").value = current?.comment || "";
    $("receipt-warehouse").value = current?.warehouse_id || warehouses.find((row) => row.is_default)?.id || "";
    $("receipt-reason").value = current?.reason_code || "stock_adjustment";
    $("supply-heading").textContent = current ? `Приход ${current.number}` : "Новый приход";
    $("supply-meta").textContent = current
      ? `${labels[current.status]} · ${current.warehouse_name} · Создан ${current.created_at}${current.posted_at ? " · Проведён " + current.posted_at + " · " + (current.posted_by || "") : ""}${current.cancelled_at ? " · Отменён " + current.cancelled_at : ""}`
      : "";
    message("", true);
    renderItems();
    if (!$("supply-dialog").open) $("supply-dialog").showModal();
  }
  function renderItems() {
    const posted = current?.status === "posted";
    const manual = editorType === "manual_receipt";
    $("manual-fields").hidden = false;
    $("receipt-reason-field").hidden = !manual;
    $("title-field").hidden = manual;
    $("title").required = !manual;
    $("receipt-warehouse").disabled = Boolean(current);
    $("receipt-reason").disabled = manual && Boolean(current) && current.status !== "draft";
    $("add-item").hidden = Boolean(
      current && (manual ? current.status !== "draft" : !["draft", "posted"].includes(current.status)),
    );
    $("title").disabled = posted && (!isAdmin || manual);
    $("comment").disabled = posted && manual;
    $("draft-actions").hidden = posted;
    $("save-first-hint").hidden = Boolean(current);
    $("save-supply").hidden = posted && (!isAdmin || manual);
    $("save-supply").textContent = posted
      ? "Сохранить реквизиты"
      : "Сохранить черновик";
    $("post-supply").hidden = posted || current?.status === "cancelled";
    $("post-supply").textContent = manual ? "Провести приход" : "Провести поставку";
    $("save-first-hint").textContent = manual
      ? "Добавьте товары и укажите количество до проведения прихода."
      : "Добавьте товары и укажите количество до проведения поставки.";
    $("preview-hint").textContent = manual
      ? "«Было» и «Станет» до проведения прихода — предварительные значения"
      : "«Было» и «Станет» до проведения поставки — предварительные значения";
    $("delete-supply").hidden = !current || (!manual && !isAdmin) || current.status === "cancelled";
    $("delete-supply").textContent = manual && posted ? "Отменить приход" : manual ? "Удалить черновик" : "Удалить поставку";
    $("items").querySelector("tbody").innerHTML = items
      .map(
        (i, index) =>
          `<tr><td>${image(i.image_url)}</td><td>${esc(i.name)}</td><td>${esc(i.article)}</td><td>${esc(i.brand)}</td><td>${number(i.stock_before ?? i.stock)}</td><td>${posted ? number(i.quantity) : `<input type="number" min="1" max="2147483647" step="1" data-quantity="${index}" ${busy ? "disabled" : ""} aria-label="Приход ${esc(i.name)}" value="${esc(i.quantity)}">`}</td><td data-after="${index}">${number(posted ? i.stock_after : (i.stock_before ?? i.stock) + Number(i.quantity))}</td><td>${posted ? "" : `<button class="button" data-remove="${index}" aria-label="Удалить ${esc(i.name)}">×</button>`}</td></tr>`,
      )
      .join("");
    $("supply-totals").textContent =
      `Позиций: ${items.length} · Единиц: ${number(items.reduce((sum, i) => sum + Number(i.quantity), 0))}`;
  }
  async function save() {
    if (editorType === "manual_receipt") {
      if (!$("receipt-warehouse").value) throw new Error("Выберите склад.");
      if (!items.length) throw new Error("Добавьте хотя бы один товар.");
      if ($("receipt-reason").value === "other" && !$("comment").value.trim())
        throw new Error("Для причины «Другое» укажите комментарий.");
      if (items.some((item) => !Number.isInteger(item.quantity) || item.quantity <= 0))
        throw new Error("Количество должно быть целым положительным числом.");
      const body = JSON.stringify({
        warehouse_id: $("receipt-warehouse").value,
        reason_code: $("receipt-reason").value,
        comment: $("comment").value,
        items: items.map((item) => ({ product_id: Number(item.product_id ?? item.id), quantity: item.quantity })),
      });
      current = await api(current ? "manual/" + encodeURIComponent(current.id) : "manual", {
        method: current ? "PATCH" : "POST",
        headers: current ? {} : { "Idempotency-Key": crypto.randomUUID() },
        body,
      });
      items = current.items.map((item) => ({ ...item }));
      return current;
    }
    if (!$("title").value.trim()) throw new Error("Укажите название поставки.");
    if (items.some((i) => !Number.isInteger(i.quantity) || i.quantity <= 0))
      throw new Error("Количество должно быть целым положительным числом.");
    if (!current)
      current = await api("supplies", {
        method: "POST",
        body: JSON.stringify({
          title: $("title").value,
          comment: $("comment").value,
          warehouse_id: $("receipt-warehouse").value,
        }),
      });
    current = await api("supplies/" + encodeURIComponent(current.id), {
      method: "PATCH",
      body: JSON.stringify({
        title: $("title").value,
        comment: $("comment").value,
        items: items.map((i) => ({
          product_id: Number(i.product_id ?? i.id),
          quantity: i.quantity,
        })),
      }),
    });
    items = current.items;
    return current;
  }
  document.querySelectorAll("[data-tab]").forEach(
    (b) =>
      (b.onclick = () =>
        action(async () => {
          tab = b.dataset.tab;
          restoreColumns();
          history.replaceState(null, "", "?tab=" + tab);
          await load();
        })),
  );
  $("filters").onsubmit = (e) => {
    e.preventDefault();
    page = 1;
    render();
  };
  $("filters").onreset = () =>
    setTimeout(() => {
      page = 1;
      render();
    }, 0);
  $("columns").onchange = (e) => {
    if (e.target.dataset.col !== undefined) {
      const key = e.target.dataset.col;
      e.target.checked ? hidden.delete(key) : hidden.add(key);
      try {
        localStorage.setItem(storageKey(), JSON.stringify([...hidden]));
      } catch {
        /* Storage is optional. */
      }
      render();
    }
  };
  $("page-size").onchange = () => {
    page = 1;
    render();
  };
  $("previous").onclick = () => {
    page--;
    render();
  };
  $("next").onclick = () => {
    page++;
    render();
  };
  const addMenu = document.querySelector(".add-menu");
  document.addEventListener("click", (event) => {
    if (addMenu?.open && !addMenu.contains(event.target)) addMenu.open = false;
  });
  $("new-supply").onclick = () => {
    addMenu.open = false;
    action(() => openSupply());
  };
  $("new-receipt").onclick = () => {
    addMenu.open = false;
    action(() => openManualReceipt());
  };
  $("close-supply").onclick = () => $("supply-dialog").close();
  $("close-source").onclick = () => $("source-dialog").close();
  $("records").onclick = (e) => {
    if (e.target.closest("[data-sort-date]")) {
      sortDirection = sortDirection === "desc" ? "asc" : "desc";
      render();
      return;
    }
    const b = e.target.closest("[data-open]");
    if (!b) return;
    const r = rows.find((i) => String(i.id) === b.dataset.open);
    action(async () => {
      if (tab === "supplies" || r.source_type === "supply") {
        await openSupply(r.source_id || r.id);
        return;
      }
      if (tab === "receipts" || r.source_type === "manual_receipt") {
        await openManualReceipt(r.id || r.source_id);
        return;
      }
      $("source-content").innerHTML =
        `<p>${esc(r.title)}</p><p>${esc(r.name)} · ${esc(r.article)}</p><p>${esc(r.comment)}</p><p>Было: ${number(r.stock_before)} · Изменение: +${number(r.quantity)} · Стало: ${number(r.stock_after)}</p>` +
        (r.source_type === "sale_cancellation"
          ? `<a href="/sales?source=all&q=${encodeURIComponent(r.sale_number || r.source_id)}">Открыть продажу</a>`
          : r.source_type === "legacy_excel"
            ? `<a href="/products/receipts/${encodeURIComponent(r.source_id)}">Открыть исходный Excel-приход</a>`
            : "");
      $("source-dialog").showModal();
    });
  };
  $("items").oninput = (e) => {
    const n = e.target.dataset.quantity;
    if (n === undefined) return;
    items[n].quantity = Number(e.target.value);
    $("items").querySelector(`[data-after="${n}"]`).textContent = number(
      (items[n].stock_before ?? items[n].stock) + items[n].quantity,
    );
    $("supply-totals").textContent =
      `Позиций: ${items.length} · Единиц: ${number(items.reduce((s, i) => s + i.quantity, 0))}`;
  };
  $("items").onclick = (e) => {
    const b = e.target.closest("[data-remove]");
    if (b) {
      items.splice(Number(b.dataset.remove), 1);
      renderItems();
    }
  };
  let addition = null;
  let pickerProduct = null,
    searchTimer = null,
    searchController = null,
    searchVersion = 0;
  function selectedProduct() {
    return pickerProduct;
  }
  function cancelSearch() {
    clearTimeout(searchTimer);
    searchVersion++;
    searchController?.abort();
  }
  async function searchProducts() {
    const version = searchVersion;
    const controller = new AbortController();
    searchController = controller;
    const parameters = new URLSearchParams({ type: "product", limit: "200" });
    const query = $("supply-product-search").value.trim();
    if (query) parameters.set("q", query);
    $("supply-search-status").textContent =
      "Ищем в " + $("supply-product-source").value.toUpperCase() + "…";
    try {
      const bitrix = $("supply-product-source").value === "bitrix";
      const products = bitrix
        ? await window.ERPProductPicker.bitrix.search(query, controller.signal)
        : await window.ERPProductPicker.request(
            "/api/v1/catalog/options?" + parameters,
            { signal: controller.signal },
          );
      if (version !== searchVersion || controller.signal.aborted) return;
      window.ERPProductPicker.results(
        $("supply-product-results"),
        products.map((p) => ({
          ...p,
          source_label: bitrix ? "Bitrix" : "Уже в ERP",
        })),
        async (p) => {
          if (busy || addition) return;
          cancelSearch();
          const selectionVersion = searchVersion;
          pickerProduct = null;
          updateAddition();
          try {
            if (bitrix) {
              p = await window.ERPProductPicker.bitrix.preview(p.bitrix_id);
              if (selectionVersion !== searchVersion) return;
              if (p.ambiguous)
                throw new Error(
                  "Найдено несколько карточек ERP. Требуется ручное сопоставление.",
                );
              p = {
                ...p,
                stock: undefined,
                source_label: p.existing ? "Уже в ERP" : "Bitrix",
                id: p.existing?.id,
              };
              for (const kind of ["brand", "category"]) {
                const select = $("supply-" + kind);
                select.replaceChildren(new Option("Выберите значение", ""));
                if (!p.existing && p[kind] && !p[kind + "_id"]) {
                  const options = await window.ERPProductPicker.request(
                    "/api/v1/catalog/options?type=" + kind + "&limit=200",
                  );
                  if (selectionVersion !== searchVersion) return;
                  options.forEach((option) =>
                    select.add(new Option(option.name, option.id)),
                  );
                }
                select.hidden = Boolean(
                  p.existing || !p[kind] || p[kind + "_id"],
                );
                document.querySelector(
                  'label[for="supply-' + kind + '"]',
                ).hidden = select.hidden;
              }
            }
            if (selectionVersion !== searchVersion) return;
            pickerProduct = p;
            window.ERPProductPicker.highlight(
              $("supply-product-results"),
              bitrix ? p.bitrix_id : p.id,
            );
            $("supply-search-status").textContent = p.source_label;
            $("add-item-message").hidden = true;
            updateAddition();
          } catch (error) {
            if (selectionVersion === searchVersion)
              $("supply-search-status").textContent = error.message;
          }
        },
        bitrix ? "bitrix_id" : "id",
      );
      $("supply-search-status").textContent = products.length
        ? "Показано товаров: " + products.length
        : bitrix
          ? "Товары не найдены. Введите название, артикул или Bitrix ID."
          : "Товар не найден в ERP. Выберите источник Bitrix для поиска и импорта.";
    } catch (error) {
      if (error.name === "AbortError" || version !== searchVersion) return;
      $("supply-search-status").textContent = error.message;
    }
  }
  $("supply-product-search").oninput = () => {
    cancelSearch();
    pickerProduct = null;
    $("supply-product-results").replaceChildren();
    updateAddition();
    $("supply-search-status").textContent =
      "Ищем в " + $("supply-product-source").value.toUpperCase() + "…";
    searchTimer = setTimeout(searchProducts, 300);
  };
  $("supply-product-source").onchange = $("supply-product-search").oninput;
  function updateAddition() {
    $("add-item").disabled = busy;
    $("create-manual-supply-product").disabled = busy || Boolean(addition);
    $("supply-product-source").disabled = busy || Boolean(addition);
    $("supply-taxonomy").hidden =
      !pickerProduct?.bitrix_id || Boolean(pickerProduct?.existing);
    $("add-quantity").disabled = busy || Boolean(addition);
    $("supply-product-search").disabled = busy || Boolean(addition);
    $("quantity-minus").disabled =
      busy || Boolean(addition) || Number($("add-quantity").value) <= 1;
    $("quantity-plus").disabled =
      busy ||
      Boolean(addition) ||
      Number($("add-quantity").value) >= 2147483647;
    $("supply-selection-empty").hidden = Boolean(selectedProduct() || addition);
    $("supply-selection-controls").hidden = !selectedProduct() && !addition;
    $("confirm-add-item").disabled =
      busy ||
      (!addition && (!selectedProduct() || !$("add-quantity").validity.valid));
    $("confirm-add-item").textContent = addition
      ? "Проверить предыдущую операцию"
      : pickerProduct?.bitrix_id && !pickerProduct?.id
        ? "Импортировать и добавить"
        : "Добавить в поставку";
    if (addition) {
      $("selected-product").textContent =
        `${addition.name}: +${addition.payload.quantity} шт.`;
      $("duplicate-confirmation").textContent =
        "Проверка результата предыдущей операции.";
      return;
    }
    const p = selectedProduct(),
      q = Number($("add-quantity").value);
    window.ERPProductPicker.preview($("selected-product"), p);
    const existing = items.find((i) => Number(i.product_id) === Number(p?.id));
    $("duplicate-confirmation").textContent = existing
      ? `Этот товар уже есть в поставке. Сейчас: ${number(existing.quantity)} шт. Добавить ещё ${number(q)} шт.?`
      : current?.status === "draft"
        ? "Товар добавится в черновик. Остаток изменится при проведении."
        : "";
  }
  $("add-quantity").oninput = updateAddition;
  for (const [id, change] of [
    ["quantity-minus", -1],
    ["quantity-plus", 1],
  ]) {
    $(id).onclick = () => {
      const q = Number($("add-quantity").value);
      if (Number.isInteger(q))
        $("add-quantity").value = Math.max(1, Math.min(2147483647, q + change));
      updateAddition();
    };
  }
  $("add-item").onclick = () =>
    action(async () => {
      // Persist an uncertain request across refresh. Retrying keeps its original payload and key.
      if (
        editorType === "supply" &&
        (!current || current.status === "draft") &&
        (!current ||
          JSON.stringify(items) !== JSON.stringify(current.items) ||
          $("title").value !== current.title ||
          $("comment").value !== current.comment)
      ) {
        await save();
      }
      cancelSearch();
      pickerProduct = null;
      $("supply-product-search").value = "";
      $("supply-product-results").replaceChildren();
      const saved = sessionStorage.getItem("supply-add:" + current.id);
      addition = saved ? JSON.parse(saved) : null;
      $("add-item-message").hidden = !addition;
      $("add-item-message").textContent = addition
        ? "Повторите подтверждение предыдущей операции. Повторный приход исключён."
        : "";
      $("confirm-add-item").textContent = addition
        ? "Проверить предыдущую операцию"
        : "Добавить в поставку";
      $("add-quantity").value = addition?.payload.quantity || 1;
      updateAddition();
      $("add-item-dialog").showModal();
      if (!addition) {
        searchProducts();
      }
    }, true).then(() => {
      if ($("add-item-dialog").open && !addition)
        $("supply-product-search").focus();
    });
  $("create-manual-supply-product").onclick = () => {
    window.ERPManualProduct.open((product) => {
      cancelSearch();
      pickerProduct = { ...product, cardResolved: true };
      $("supply-product-source").value = "erp";
      $("supply-product-results").replaceChildren();
      $("add-item-message").hidden = true;
      updateAddition();
    });
  };
  $("close-add-item").onclick = $("cancel-add-item").onclick = () => {
    if (!busy) $("add-item-dialog").close();
  };
  $("add-item-dialog").addEventListener("cancel", (event) => {
    if (busy) event.preventDefault();
  });
  $("add-item-dialog").addEventListener("close", () => {
    cancelSearch();
    pickerProduct = null;
  });
  $("add-item-form").onsubmit = async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    updateAddition();
    $("confirm-add-item").disabled = true;
    try {
      const p = selectedProduct();
      if (!addition) {
        const quantity = Number($("add-quantity").value);
        if (!p) throw new Error("Выберите товар из каталога ERP.");
        if (
          !Number.isInteger(quantity) ||
          quantity <= 0 ||
          quantity > 2147483647
        )
          throw new Error("Количество должно быть целым положительным числом.");
        if (p.bitrix_id) {
          updateAddition();
          const imported = await window.ERPProductPicker.bitrix.import(
            p.bitrix_id,
            {
              supply_id: current?.id || null,
              brand_id: p.brand_id || $("supply-brand").value || null,
              category_id: p.category_id || $("supply-category").value || null,
            },
          );
          p.id = imported.erp_product_id;
          p.cardResolved = true;
        }
        if (editorType === "manual_receipt") {
          const existing = items.find((item) => Number(item.product_id ?? item.id) === Number(p.id));
          if (existing) existing.quantity = Number(existing.quantity) + quantity;
          else items.push({ ...p, product_id: Number(p.id), quantity, stock_before: Number(p.stock || 0) });
          $("add-item-dialog").close();
          renderItems();
          message(`${p.name} добавлен в приход: +${quantity} шт.`, true);
          return;
        }
        addition = {
          cardResolved: Boolean(p.cardResolved),
          key:
            crypto.randomUUID?.() ||
            Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
              byte.toString(16).padStart(2, "0"),
            ).join(""),
          name: p.name,
          payload: { product_id: Number(p.id), quantity },
        };
        sessionStorage.setItem(
          "supply-add:" + current.id,
          JSON.stringify(addition),
        );
      }
      updateAddition();
      const result = await api(
        "supplies/" + encodeURIComponent(current.id) + "/items",
        {
          method: "POST",
          headers: { "Idempotency-Key": addition.key },
          body: JSON.stringify(addition.payload),
        },
      );
      const success = `${addition.name} добавлен в поставку ${result.number}: +${addition.payload.quantity} шт.${result.status === "draft" ? " Остаток изменится при проведении." : ""}`;
      sessionStorage.removeItem("supply-add:" + current.id);
      addition = null;
      current = result;
      items = result.items.map((item) => ({ ...item }));
      $("add-item-dialog").close();
      message(success, true);
      await load();
    } catch (error) {
      const cardResolved =
        addition?.cardResolved || pickerProduct?.cardResolved;
      if ([403, 422].includes(error.status)) {
        sessionStorage.removeItem("supply-add:" + current.id);
        addition = null;
      }
      updateAddition();
      $("add-item-message").textContent =
        (cardResolved
          ? "Товар сохранён в ERP, но добавление в поставку не подтверждено. "
          : "") + error.message;
      $("add-item-message").hidden = false;
    } finally {
      busy = false;
      renderItems();
      updateAddition();
    }
  };
  $("save-supply").onclick = () =>
    action(async () => {
      if (editorType === "supply" && current?.status === "posted") {
        current = await api(
          "supplies/" + encodeURIComponent(current.id) + "/details",
          {
            method: "PATCH",
            body: JSON.stringify({
              title: $("title").value,
              comment: $("comment").value,
            }),
          },
        );
        items = current.items.map((item) => ({ ...item }));
      } else {
        await save();
      }
      await load();
      if (editorType === "manual_receipt") await openManualReceipt(current.id);
      else await openSupply(current.id);
      message(
        current.status === "posted"
          ? "Название и комментарий сохранены. Остаток не изменён."
          : "Черновик сохранён. Остаток не изменён.",
        true,
      );
    }, true);
  $("post-supply").onclick = () =>
    action(async () => {
      await save();
      const id = current.id;
      await api((editorType === "manual_receipt" ? "manual/" : "supplies/") + encodeURIComponent(id) + "/post", {
        method: "POST",
      });
      await load();
      if (editorType === "manual_receipt") await openManualReceipt(id);
      else await openSupply(id);
      message(editorType === "manual_receipt" ? "Приход проведён. Остатки обновлены." : "Поставка проведена. Остатки обновлены.", true);
    }, true);
  const deleteLabels = {
    DRAFT_DELETE: "Черновик будет удалён",
    WILL_DECREASE: "Остаток будет уменьшен",
    SKIPPED_DUE_TO_LATER_SALE: "Была продажа",
    CONFLICT_NEGATIVE_STOCK: "Удаление заблокировано",
  };
  function renderDeletePreview(preview) {
    $("delete-preview-warning").hidden = !preview.skipped_positions;
    $("delete-preview-warning").textContent = preview.skipped_positions
      ? "После проведения этой поставки часть товаров участвовала в продаже. Остаток этих позиций изменён не будет. Продажи останутся без изменений."
      : "";
    $("delete-preview-items").innerHTML = preview.items
      .map((item) => {
        const sales = (item.sales || [])
          .map(
            (sale) =>
              `<a href="/sales?source=all&q=${encodeURIComponent(sale.number)}">Продажа ${esc(sale.number)}${sale.date ? " от " + esc(sale.date) : ""}</a>`,
          )
          .join(", ");
        const kind =
          item.status === "SKIPPED_DUE_TO_LATER_SALE"
            ? "skipped"
            : item.status === "CONFLICT_NEGATIVE_STOCK"
              ? "conflict"
              : "";
        const product = `<div class="delete-product">${image(item.image_url)}<div><strong>${esc(item.product_name)}</strong><small>Артикул: ${esc(item.article || "—")}</small></div></div>`;
        const row = `<tr><td>${product}</td><td>${number(item.quantity)} шт.</td><td>${number(item.current_stock)} шт.</td><td>${number(item.stock_after)} шт.${item.status === "WILL_DECREASE" ? `<small class="delete-sales">↓ −${number(item.quantity)}</small>` : ""}</td><td><span class="delete-status ${kind}">${esc(deleteLabels[item.status] || item.status)}</span>${sales ? `<span class="delete-sales">${sales}</span>` : ""}</td></tr>`;
        const warning =
          item.status === "SKIPPED_DUE_TO_LATER_SALE"
            ? '<tr class="delete-row-warning"><td colspan="5">После проведения этой поставки товар участвовал в продаже. Остаток позиции изменён не будет. Продажа останется без изменений.</td></tr>'
            : "";
        return row + warning;
      })
      .join("");
    $("delete-total-positions").textContent = number(
      preview.decreased_positions,
    );
    $("delete-total-quantity").textContent =
      "−" + number(preview.decreased_quantity);
    $("delete-total-skipped").textContent = number(preview.skipped_positions);
    $("confirm-delete").disabled = Boolean(preview.has_conflicts);
    $("delete-preview-message").hidden = !preview.has_conflicts;
    $("delete-preview-message").textContent = preview.has_conflicts
      ? "Удаление невозможно: по одной или нескольким позициям остаток станет отрицательным."
      : "";
  }
  $("delete-supply").onclick = () => {
    if (editorType === "manual_receipt") {
      action(async () => {
        const posted = current.status === "posted";
        if (!window.confirm(posted
          ? "Отменить проведённый приход обратным движением?"
          : "Удалить черновик прихода?")) return;
        await api("manual/" + encodeURIComponent(current.id) + (posted ? "/cancel" : ""), {
          method: posted ? "POST" : "DELETE",
        });
        $("supply-dialog").close();
        await load();
        message(posted ? "Приход отменён. Создано обратное движение." : "Черновик прихода удалён.");
      }, true);
      return;
    }
    $("delete-confirm-heading").textContent =
      `Удалить поставку ${current.number}?`;
    $("delete-confirm-text").textContent =
      current.status === "draft"
        ? "Вы собираетесь удалить черновик поставки. ERP проверит позиции и подтвердит, что складские остатки не изменятся."
        : "Вы собираетесь удалить проведённую поставку. ERP проверит все позиции и покажет, как удаление повлияет на товары и продажи.";
    $("delete-confirm-message").hidden = true;
    $("delete-confirm-dialog").showModal();
  };
  document.querySelectorAll("[data-close-delete]").forEach((button) => {
    button.onclick = () => $("delete-confirm-dialog").close();
  });
  document.querySelectorAll("[data-close-preview]").forEach((button) => {
    button.onclick = () => $("delete-preview-dialog").close();
  });
  for (const id of ["delete-confirm-dialog", "delete-preview-dialog"]) {
    $(id).addEventListener("click", (event) => {
      if (event.target === $(id)) $(id).close();
    });
  }
  $("continue-delete").onclick = async () => {
    if (busy || !current) return;
    busy = true;
    $("continue-delete").disabled = true;
    $("delete-confirm-message").hidden = true;
    try {
      const preview = await api(
        "supplies/" + encodeURIComponent(current.id) + "/delete-preview",
      );
      renderDeletePreview(preview);
      $("delete-confirm-dialog").close();
      $("delete-preview-dialog").showModal();
    } catch (error) {
      $("delete-confirm-message").textContent = error.message;
      $("delete-confirm-message").hidden = false;
    } finally {
      busy = false;
      $("continue-delete").disabled = false;
    }
  };
  $("confirm-delete").onclick = async () => {
    if (busy || !current) return;
    busy = true;
    $("confirm-delete").disabled = true;
    $("delete-preview-message").hidden = true;
    try {
      await api("supplies/" + encodeURIComponent(current.id), {
        method: "DELETE",
      });
      $("delete-preview-dialog").close();
      $("supply-dialog").close();
      await load();
      message("Поставка удалена. Остатки пересчитаны по результатам проверки.");
    } catch (error) {
      if (error.data) renderDeletePreview(error.data);
      $("delete-preview-message").textContent = error.message;
      $("delete-preview-message").hidden = false;
      $("confirm-delete").disabled = Boolean(error.data?.has_conflicts);
    } finally {
      busy = false;
    }
  };
  $("excel").onchange = () =>
    action(async () => {
      const file = $("excel").files[0];
      if (!file) return;
      const data = new FormData();
      data.append("file", file);
      const supply = await api("excel", { method: "POST", body: data });
      tab = "supplies";
      await load();
      await openSupply(supply.id);
      message(
        "Excel загружен в черновик. Проверьте позиции и проведите поставку.",
        true,
      );
      $("excel").value = "";
    });
  action(load);
})();
