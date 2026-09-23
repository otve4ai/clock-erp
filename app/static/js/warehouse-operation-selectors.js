(() => {
  const addField = (form, before) => {
    if (!form || form.querySelector("[data-operational-warehouse]")) return;
    const wrap = document.createElement("div"); wrap.className = "field receipt-document-field";
    const label = document.createElement("label"); label.textContent = "Склад *";
    const select = document.createElement("select");
    select.name = "warehouse_id"; select.required = true; select.dataset.operationalWarehouse = "";
    select.className = "control field"; select.append(new Option("Загрузка…", ""));
    label.append(select); wrap.append(label); (before || form).before ? (before || form).before(wrap) : form.prepend(wrap);
  };
  const inventoryForm = document.getElementById("startForm");
  if (inventoryForm) addField(inventoryForm, inventoryForm.querySelector("button[type=submit]"));
  const receiptForm = document.getElementById("receiptForm");
  if (receiptForm) addField(receiptForm, receiptForm.querySelector(".receipt-document-title"));
  const fields = [...document.querySelectorAll("[data-operational-warehouse]")];
  if (!fields.length) return;
  fetch("/api/v1/warehouses", {headers: {Accept: "application/json"}})
    .then(response => response.json().then(body => {
      if (!response.ok || body.ok === false) throw new Error(body.message || "Склады недоступны");
      return body.warehouses;
    }))
    .then(warehouses => fields.forEach(field => {
      field.replaceChildren();
      let chosen = false;
      warehouses.forEach(warehouse => {
        const selected = warehouse.selected && !chosen;
        if (selected) chosen = true;
        const option = new Option(warehouse.name, warehouse.id, selected, selected);
        field.add(option);
      });
      if (field.selectedOptions.length !== 1 && field.options.length) field.selectedIndex = 0;
    }))
    .catch(error => fields.forEach(field => {
      field.replaceChildren(new Option("Не удалось загрузить склады", ""));
      field.setCustomValidity(error.message);
    }));
  if (inventoryForm) {
    const originalFetch = window.fetch.bind(window);
    window.fetch = (url, options = {}) => {
      if (url === "/api/v1/inventories" && options.method === "POST") {
        const payload = JSON.parse(options.body || "{}");
        payload.warehouse_id = inventoryForm.elements.warehouse_id?.value;
        options = {...options, body: JSON.stringify(payload)};
      }
      return originalFetch(url, options);
    };
  }
})();
