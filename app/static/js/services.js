(function () {
    "use strict";
    var boot = window.SERVICES_BOOTSTRAP || {};
    var state = {services: boot.services || [], categories: boot.categories || [], filter: "all", query: "", archived: false, viewUserId: 0, viewUser: null};
    var grid = document.getElementById("serviceGrid");
    var empty = document.getElementById("serviceEmpty");
    var count = document.getElementById("serviceCount");
    var toast = document.getElementById("servicesToast");
    var passwordTimers = new Map();

    function escapeHtml(value) {
        return String(value == null ? "" : value).replace(/[&<>'"]/g, function (character) {
            return {"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[character];
        });
    }
    function notify(message, error) {
        toast.textContent = message;
        toast.className = "services-toast is-visible" + (error ? " is-error" : "");
        clearTimeout(notify.timer);
        notify.timer = setTimeout(function () { toast.className = "services-toast"; }, 2600);
    }
    async function api(url, options) {
        options = options || {};
        options.headers = Object.assign({}, options.headers || {}, options.method && options.method !== "GET" ? {"X-CSRF-Token": boot.csrf} : {});
        var response = await fetch(url, options);
        var payload = await response.json().catch(function () { return {error:"Ошибка сервера"}; });
        if (!response.ok) throw new Error(payload.error || "Ошибка сервера");
        return payload;
    }
    function categoryLabel(value) {
        var category = state.categories.find(function(item) { return item.key === value; });
        return category ? category.name : value;
    }
    function iconUrl(service) {
        return "/api/services/" + service.id + "/icon?v=" + encodeURIComponent(service.version || 0);
    }
    function iconMarkup(service) {
        if (service.has_custom_icon) return '<img src="' + iconUrl(service) + '" alt="">';
        var icons = {globe:"◎",cart:"₽",truck:"→",server:"▦",cloud:"☁",lock:"◇"};
        return escapeHtml(icons[service.icon] || service.name.slice(0, 1).toUpperCase());
    }
    function accountMarkup(service, account) {
        var login = account.has_login && (service.permissions.can_view_login || service.permissions.can_copy_login) ?
            '<div class="credential-row"><span class="credential-label">Логин</span><span class="credential-value" data-login="' + account.id + '">' + (service.permissions.can_view_login ? 'Загрузка…' : 'Скрыт') + '</span><span class="credential-actions">' +
            (service.permissions.can_copy_login ? '<button type="button" data-copy-login="' + account.id + '" aria-label="Копировать логин">⧉</button>' : '') + '</span></div>' : '';
        var password = account.has_password && (service.permissions.can_view_password || service.permissions.can_copy_password) ?
            '<div class="credential-row"><span class="credential-label">Пароль</span><span class="credential-value" data-password="' + account.id + '">••••••••••••</span><span class="credential-actions">' + (service.permissions.can_view_password ? '<button type="button" data-show-password="' + account.id + '" aria-label="Показать пароль">◉</button>' : '') +
            (service.permissions.can_copy_password ? '<button type="button" data-copy-password="' + account.id + '" aria-label="Копировать пароль">⧉</button>' : '') + '</span></div>' : '';
        return '<div class="service-account"><div class="service-account-title">' + escapeHtml(account.label) + '</div>' + login + password + '</div>';
    }
    function accessMarkup(service) {
        if (!state.viewUserId) {
            if (typeof service.access_count === "number") {
                return '<div class="service-access">Доступ имеют: ' + service.access_count + ' сотрудников</div>';
            }
            return '<div class="service-access">' + (service.permissions.can_view_password ? 'Доступ к реквизитам разрешён' : 'Доступ ограничен') + '</div>';
        }
        var labels = [["can_open","Открывает"],["can_view_login","Видит логин"],["can_copy_login","Копирует логин"],["can_view_password","Видит пароль"],["can_copy_password","Копирует пароль"]];
        var chips = labels.filter(function(item) { return service.permissions[item[0]]; }).map(function(item) { return '<span>' + item[1] + '</span>'; }).join("");
        return '<div class="employee-access"><strong>Доступ сотрудника</strong><div>' + (chips || '<span>Только видит карточку</span>') + '</div></div>';
    }
    function cardMarkup(service, index) {
        var accounts = service.accounts.map(function (account) { return accountMarkup(service, account); }).join("");
        if (!accounts) accounts = '<div class="service-access">Без логина и пароля</div>';
        var overflow = !state.viewUserId && !service.archived && service.permissions.can_archive ? '<div class="service-card-overflow"><button class="service-overflow-toggle" type="button" data-service-overflow="' + service.id + '" aria-expanded="false" aria-haspopup="menu" aria-label="Другие действия">…</button><div class="service-overflow-menu" role="menu" hidden><button type="button" role="menuitem" data-archive="' + service.id + '">В архив</button></div></div>' : '';
        var controls = state.viewUserId ?
            (!service.archived && service.permissions.can_open ? '<button class="service-open service-action-primary" type="button" data-open="' + service.id + '">Открыть</button>' : '') : service.archived ?
            (service.permissions.can_archive ? '<button class="minor" type="button" data-restore="' + service.id + '">Восстановить</button>' : '') +
            (boot.isOwner ? '<button class="minor danger" type="button" data-delete-permanent="' + service.id + '">Удалить навсегда</button>' : '') :
            (service.permissions.can_open ? '<button class="service-open service-action-primary" type="button" data-open="' + service.id + '">Открыть</button>' : '') +
            (service.permissions.can_edit ? '<button class="minor service-action-secondary" type="button" data-edit="' + service.id + '">' + (service.permissions.can_manage_access ? 'Управлять' : 'Изменить') + '</button>' : '') + overflow;
        var move = !state.viewUserId && !service.archived && state.filter === "all" && !state.query ? '<button class="move-button" type="button" data-move="up" data-id="' + service.id + '" aria-label="Переместить выше" ' + (index === 0 ? 'disabled' : '') + '>↑</button><button class="move-button" type="button" data-move="down" data-id="' + service.id + '" aria-label="Переместить ниже">↓</button>' : '';
        return '<article class="service-card' + (service.archived ? ' is-archived' : '') + '" data-id="' + service.id + '"><div class="service-card-head"><div class="service-icon">' + iconMarkup(service) + '</div><div><h2>' + escapeHtml(service.name) + '</h2><span class="service-domain" title="' + escapeHtml(service.url) + '">' + escapeHtml(service.domain) + '</span></div>' + (!service.archived && !state.viewUserId ? '<button type="button" class="favorite-button' + (service.favorite ? ' is-active' : '') + '" data-favorite="' + service.id + '" aria-label="' + (service.favorite ? 'Удалить из избранного' : 'Добавить в избранное') + '" title="' + (service.favorite ? 'Удалить из избранного' : 'Добавить в избранное') + '">' + (service.favorite ? '★' : '☆') + '</button>' : '<span></span>') + '</div><p class="service-description">' + escapeHtml(service.description || "Без описания") + '</p><span class="service-category">' + escapeHtml(categoryLabel(service.category)) + '</span>' + accounts + accessMarkup(service) + '<div class="service-card-actions">' + move + controls + '</div></article>';
    }
    function visibleServices() {
        var term = state.query.trim().toLocaleLowerCase("ru");
        return state.services.filter(function (service) {
            var filterMatch = state.filter === "all" || (state.filter === "favorite" ? service.favorite : service.category === state.filter);
            var haystack = [service.name, service.description, service.category, service.url].concat(service.accounts.map(function (item) { return item.label; })).join(" ").toLocaleLowerCase("ru");
            return filterMatch && (!term || haystack.indexOf(term) !== -1);
        });
    }
    function render() {
        hideAllPasswords(false);
        var visible = visibleServices();
        grid.innerHTML = visible.map(cardMarkup).join("");
        count.textContent = visible.length + " " + (visible.length === 1 ? "сервис" : "сервисов");
        var summary = document.getElementById("accessSummary");
        if (summary) {
            var passwordCount = visible.reduce(function(total, service) { return total + service.accounts.filter(function(account) { return account.has_password && (service.permissions.can_view_password || service.permissions.can_copy_password); }).length; }, 0);
            summary.textContent = state.viewUser ? state.viewUser.display_name + " · доступ к " + passwordCount + " паролям" : "";
        }
        empty.hidden = visible.length !== 0;
        if (!visible.length) {
            empty.querySelector("h2").textContent = state.services.length ? "Ничего не найдено" : (state.archived ? "Архив пуст" : "Сервисы пока не добавлены");
            empty.querySelector("p").textContent = state.services.length ? "Измените запрос или фильтр." : "Добавьте первый рабочий сайт или приложение.";
        }
        visible.forEach(function (service) {
            if (service.archived || !service.permissions.can_view_login) return;
            service.accounts.filter(function (account) { return account.has_login; }).forEach(loadLogin);
        });
    }
    async function loadLogin(account) {
        try {
            var payload = await api("/api/service-accounts/" + account.id + "/login");
            var node = document.querySelector('[data-login="' + account.id + '"]');
            if (node) { node.textContent = payload.value || "—"; node.dataset.value = payload.value || ""; }
        } catch (error) {
            var failed = document.querySelector('[data-login="' + account.id + '"]');
            if (failed) failed.textContent = "Недоступен";
        }
    }
    function hidePassword(accountId, announce) {
        var node = document.querySelector('[data-password="' + accountId + '"]');
        if (node) { node.textContent = "••••••••••••"; delete node.dataset.value; }
        clearTimeout(passwordTimers.get(String(accountId)));
        passwordTimers.delete(String(accountId));
        if (announce) notify("Пароль скрыт");
    }
    function hideAllPasswords(announce) {
        Array.from(document.querySelectorAll("[data-password]")).forEach(function (node) { hidePassword(node.dataset.password, announce); });
    }
    async function showPassword(accountId) {
        var node = document.querySelector('[data-password="' + accountId + '"]');
        if (!node) return;
        if (node.dataset.value) { hidePassword(accountId, true); return; }
        try {
            var payload = await api("/api/service-accounts/" + accountId + "/password");
            node.textContent = payload.value || "—";
            node.dataset.value = payload.value || "";
            passwordTimers.set(String(accountId), setTimeout(function () { hidePassword(accountId, true); }, 15000));
        } catch (error) { notify(error.message, true); }
    }
    async function copyCredential(accountId, kind) {
        try {
            var value;
            var node = kind === "login" ? document.querySelector('[data-login="' + accountId + '"]') : null;
            value = node && node.dataset.value;
            if (value == null) value = (await api("/api/service-accounts/" + accountId + "/copied", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:kind})})).value;
            if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("Буфер обмена недоступен");
            await navigator.clipboard.writeText(value || "");
            if (node && node.dataset.value != null) await api("/api/service-accounts/" + accountId + "/copied", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:kind})});
            notify(kind === "password" ? "Пароль скопирован" : "Логин скопирован");
            value = "";
        } catch (error) { notify(error.message || "Не удалось скопировать", true); }
    }
    async function refresh() {
        var url = "/api/services?archived=" + (state.archived ? "1" : "0");
        if (state.viewUserId) url += "&user_id=" + encodeURIComponent(state.viewUserId);
        var payload = await api(url);
        state.services = payload.services;
        if (payload.categories) state.categories = payload.categories;
        renderCategoryControls();
        render();
    }
    async function openService(id) {
        var popup = window.open("about:blank", "_blank");
        if (popup) {
            popup.opener = null;
            var referrer = popup.document.createElement("meta");
            referrer.name = "referrer"; referrer.content = "no-referrer";
            popup.document.head.appendChild(referrer);
        }
        try {
            var payload = await api("/api/services/" + id + "/open", {method:"POST"});
            if (!popup) throw new Error("Браузер заблокировал новую вкладку");
            popup.location.replace(payload.url);
        } catch (error) { if (popup) popup.close(); notify(error.message, true); }
    }
    function closeServiceOverflow(except) {
        document.querySelectorAll(".service-card-overflow").forEach(function(wrapper) {
            if (wrapper === except) return;
            wrapper.querySelector(".service-overflow-menu").hidden = true;
            wrapper.querySelector(".service-overflow-toggle").setAttribute("aria-expanded", "false");
        });
    }
    grid.addEventListener("click", async function (event) {
        var button = event.target.closest("button"); if (!button) return;
        var id;
        if (button.dataset.serviceOverflow) {
            event.stopPropagation();
            var wrapper = button.closest(".service-card-overflow"), menu = wrapper.querySelector(".service-overflow-menu"), opening = menu.hidden;
            closeServiceOverflow(wrapper); menu.hidden = !opening; button.setAttribute("aria-expanded", opening ? "true" : "false"); return;
        }
        if (button.dataset.showPassword) return showPassword(button.dataset.showPassword);
        if (button.dataset.copyPassword) return copyCredential(button.dataset.copyPassword, "password");
        if (button.dataset.copyLogin) return copyCredential(button.dataset.copyLogin, "login");
        if (button.dataset.open) return openService(button.dataset.open);
        if (button.dataset.edit) return openForm(Number(button.dataset.edit));
        if (button.dataset.deletePermanent) return openDeleteDialog(Number(button.dataset.deletePermanent));
        try {
            if (button.dataset.favorite) {
                id = Number(button.dataset.favorite); var service = state.services.find(function (item) { return item.id === id; });
                await api("/api/services/" + id + "/favorite", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({favorite:!service.favorite})}); await refresh();
            } else if (button.dataset.archive) {
                id = Number(button.dataset.archive); if (!window.confirm("Переместить сервис в архив?")) return;
                await api("/api/services/" + id + "/archive", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({archived:true})}); notify("Сервис перемещён в архив"); await refresh();
            } else if (button.dataset.restore) {
                id = Number(button.dataset.restore); await api("/api/services/" + id + "/archive", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({archived:false})}); notify("Сервис восстановлен"); await refresh();
            } else if (button.dataset.move) {
                id = Number(button.dataset.id); var index = state.services.findIndex(function (item) { return item.id === id; });
                var target = button.dataset.move === "up" ? index - 1 : index + 1; if (target < 0 || target >= state.services.length) return;
                var moved = state.services.splice(index, 1)[0]; state.services.splice(target, 0, moved); render();
                await api("/api/services/reorder", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ordered_ids:state.services.map(function(item){return item.id;})})});
            }
        } catch (error) { notify(error.message, true); }
    });
    document.getElementById("serviceSearch").addEventListener("input", function (event) { state.query = event.target.value; render(); });
    function closeDropdowns(except) {
        document.querySelectorAll(".services-dropdown").forEach(function(dropdown) {
            if (dropdown === except) return;
            dropdown.querySelector(".services-dropdown-menu").hidden = true;
            dropdown.querySelector(".services-dropdown-toggle").setAttribute("aria-expanded", "false");
        });
    }
    document.querySelectorAll(".services-dropdown").forEach(function(dropdown) {
        var toggle = dropdown.querySelector(".services-dropdown-toggle"), menu = dropdown.querySelector(".services-dropdown-menu");
        toggle.addEventListener("click", function(event) {
            event.stopPropagation();
            var opening = menu.hidden;
            closeDropdowns(dropdown);
            menu.hidden = !opening;
            toggle.setAttribute("aria-expanded", opening ? "true" : "false");
        });
        menu.addEventListener("click", function(event) { event.stopPropagation(); });
    });
    var serviceDropdown = document.getElementById("serviceDropdown");
    function serviceFilterLabel(value) {
        if (value === "all") return "Все";
        if (value === "favorite") return "Все";
        return categoryLabel(value);
    }
    function renderCategoryControls() {
        var filters = serviceDropdown.querySelector("[data-category-filters]");
        var values = [{key:"all",name:"Все"}].concat(state.categories);
        if (state.filter !== "favorite" && !values.some(function(item) { return item.key === state.filter; })) state.filter = "all";
        filters.innerHTML = values.map(function(item) { var selected = item.key === state.filter; return '<button type="button" class="' + (selected ? 'is-selected' : '') + '" data-filter="' + escapeHtml(item.key) + '">' + escapeHtml(item.name) + '<span>' + (selected ? '✓' : '') + '</span></button>'; }).join("");
        serviceDropdown.querySelector("[data-service-value]").textContent = serviceFilterLabel(state.filter);
        var favoriteToggle = document.getElementById("favoriteToggle");
        if (favoriteToggle) {
            var favoriteActive = state.filter === "favorite";
            favoriteToggle.classList.toggle("is-active", favoriteActive);
            favoriteToggle.setAttribute("aria-pressed", favoriteActive ? "true" : "false");
            favoriteToggle.querySelector("[data-favorite-icon]").textContent = favoriteActive ? "★" : "☆";
        }
        if (form && form.elements.category) {
            var selectedCategory = form.elements.category.value;
            form.elements.category.innerHTML = state.categories.map(function(item) { return '<option value="' + escapeHtml(item.key) + '">' + escapeHtml(item.name) + '</option>'; }).join("");
            if (state.categories.some(function(item) { return item.key === selectedCategory; })) form.elements.category.value = selectedCategory;
        }
    }
    serviceDropdown.querySelector(".services-dropdown-menu").addEventListener("click", function(event) {
        var button = event.target.closest("[data-filter]"); if (!button) return;
        state.filter = button.dataset.filter;
        renderCategoryControls();
        closeDropdowns(); render();
    });
    var favoriteToggle = document.getElementById("favoriteToggle");
    if (favoriteToggle) favoriteToggle.addEventListener("click", function() {
        state.filter = state.filter === "favorite" ? "all" : "favorite";
        renderCategoryControls(); render();
    });
    var manageCategories = document.getElementById("manageCategories");
    if (manageCategories) manageCategories.addEventListener("click", openCategoryDialog);
    var employeeDropdown = document.getElementById("employeeDropdown"), revokeAccess = document.getElementById("revokeAccess");
    if (employeeDropdown) {
        var employeeOptions = employeeDropdown.querySelector("[data-employee-options]");
        var activeUsers = (boot.users || []).filter(function(user) { return user.active; });
        function employeeInitials(user) { return String(user.display_name || "?").split(/\s+/).slice(0,2).map(function(part) { return part.slice(0,1); }).join("").toUpperCase(); }
        employeeOptions.innerHTML = '<button type="button" class="employee-option is-selected" data-user-id="0"><span class="employee-avatar">Вс</span><span><strong>Все сотрудники</strong><small>Без фильтра</small></span><b>✓</b></button>' + activeUsers.map(function(user) { return '<button type="button" class="employee-option" data-user-id="' + user.id + '"><span class="employee-avatar">' + escapeHtml(employeeInitials(user)) + '</span><span><strong>' + escapeHtml(user.display_name) + '</strong><small>' + escapeHtml(user.system_role === "admin" ? 'Полный доступ' : user.role_label) + '</small></span><b></b></button>'; }).join("");
        employeeOptions.addEventListener("click", async function(event) {
            var button = event.target.closest("[data-user-id]"); if (!button) return;
            var userId = Number(button.dataset.userId || 0);
            state.viewUserId = userId;
            state.viewUser = activeUsers.find(function(user) { return user.id === userId; }) || null;
            employeeDropdown.querySelector("[data-employee-value]").textContent = state.viewUser ? state.viewUser.display_name : "Все";
            employeeOptions.querySelectorAll("[data-user-id]").forEach(function(item) { var selected=item===button; item.classList.toggle("is-selected",selected); item.querySelector("b").textContent=selected?"✓":""; });
            revokeAccess.hidden = !state.viewUser || state.viewUser.system_role === "admin";
            state.archived = false;
            var archiveToggleButton = document.getElementById("archiveToggle"); if (archiveToggleButton) archiveToggleButton.textContent = "Показать архив";
            closeDropdowns();
            try { await refresh(); } catch (error) { notify(error.message, true); }
        });
        employeeDropdown.querySelector("input").addEventListener("input", function() {
            var term = this.value.trim().toLocaleLowerCase("ru");
            employeeOptions.querySelectorAll(".employee-option").forEach(function(option) { option.hidden = Boolean(term) && option.textContent.toLocaleLowerCase("ru").indexOf(term) === -1; });
        });
        var revokeDialog = document.getElementById("serviceAccessRevokeDialog");
        revokeAccess.addEventListener("click", function() {
            if (!state.viewUser || !revokeDialog) return;
            revokeDialog.querySelector("[data-revoke-employee]").textContent = state.viewUser.display_name;
            revokeDialog.querySelector("[data-revoke-result]").hidden = true;
            revokeDialog.querySelector("[data-revoke-passwords]").replaceChildren();
            revokeDialog.querySelector(".form-status").textContent = "";
            revokeDialog.querySelector("[data-revoke-confirm]").hidden = false;
            revokeDialog.querySelectorAll("[data-revoke-close]").forEach(function(button) { button.textContent = button.classList.contains("dialog-close") ? "×" : "Отмена"; });
            revokeDialog.showModal();
        });
        revokeDialog.querySelectorAll("[data-revoke-close]").forEach(function(button) { button.addEventListener("click", function() { revokeDialog.close(); }); });
        revokeDialog.querySelector("[data-revoke-confirm]").addEventListener("click", async function() {
            var button = this;
            if (!state.viewUser) return;
            button.disabled = true;
            try {
                var payload = await api("/api/services/access/users/" + state.viewUser.id + "/revoke", {method:"POST"});
                var list = revokeDialog.querySelector("[data-revoke-passwords]");
                (payload.access.passwords || []).forEach(function(item) {
                    var row = document.createElement("li");
                    row.textContent = item.service_name + " — " + item.account_label;
                    list.appendChild(row);
                });
                if (!list.children.length) {
                    var emptyItem = document.createElement("li");
                    emptyItem.textContent = "Парольные реквизиты не были доступны.";
                    list.appendChild(emptyItem);
                }
                revokeDialog.querySelector("[data-revoke-result]").hidden = false;
                button.hidden = true;
                var closeButton = Array.from(revokeDialog.querySelectorAll("[data-revoke-close]")).find(function(item) { return !item.classList.contains("dialog-close"); });
                if (closeButton) closeButton.textContent = "Готово";
                notify("Все доступы сотрудника отозваны");
                await refresh();
            } catch (error) {
                revokeDialog.querySelector(".form-status").textContent = error.message;
            } finally {
                button.disabled = false;
            }
        });
    }
    document.addEventListener("click", function() { closeDropdowns(); closeServiceOverflow(); });
    document.addEventListener("keydown", function(event) { if (event.key === "Escape") { closeDropdowns(); closeServiceOverflow(); } });
    document.addEventListener("visibilitychange", function () { if (document.hidden) hideAllPasswords(false); });
    window.addEventListener("pagehide", function () { hideAllPasswords(false); });

    var dialog = document.getElementById("serviceDialog"), form = document.getElementById("serviceForm"), accountFields = document.getElementById("accountFields"), permissionFields = document.getElementById("permissionFields");
    var categoryDialog = document.getElementById("categoryDialog"), categoryDeleteDialog = document.getElementById("categoryDeleteDialog"), pendingCategoryDelete = null;
    function renderCategoryManager() {
        if (!categoryDialog) return;
        var list = categoryDialog.querySelector("[data-category-list]");
        list.innerHTML = state.categories.map(function(category, index) {
            return '<div class="category-row" data-category-key="' + escapeHtml(category.key) + '"><div><strong>' + escapeHtml(category.name) + '</strong><small>' + category.service_count + ' ' + (category.service_count === 1 ? 'сервис' : 'сервисов') + '</small></div><div class="category-row-actions"><button type="button" data-category-move="up" aria-label="Выше" ' + (index === 0 ? 'disabled' : '') + '>↑</button><button type="button" data-category-move="down" aria-label="Ниже" ' + (index === state.categories.length - 1 ? 'disabled' : '') + '>↓</button><button type="button" data-category-rename>Изменить</button><button type="button" class="danger" data-category-delete ' + (state.categories.length <= 1 ? 'disabled' : '') + '>Удалить</button></div></div>';
        }).join("");
    }
    function openCategoryDialog() {
        if (!categoryDialog) return;
        categoryDialog.querySelector("[data-category-status]").textContent = "";
        renderCategoryManager(); categoryDialog.showModal();
    }
    async function reloadCategories() {
        var payload = await api("/api/service-categories");
        state.categories = payload.categories;
        renderCategoryControls(); renderCategoryManager(); render();
    }
    if (categoryDialog) {
        categoryDialog.querySelectorAll("[data-category-close]").forEach(function(button) { button.addEventListener("click", function() { categoryDialog.close(); }); });
        categoryDialog.querySelector("[data-add-category]").addEventListener("click", async function() {
            var input = categoryDialog.querySelector("[data-new-category]"); var name = input.value.trim(); if (!name) return input.focus();
            try { await api("/api/service-categories", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:name})}); input.value=""; await reloadCategories(); notify("Раздел добавлен"); } catch (error) { categoryDialog.querySelector("[data-category-status]").textContent=error.message; }
        });
        categoryDialog.querySelector("[data-category-list]").addEventListener("click", async function(event) {
            var row=event.target.closest("[data-category-key]"); if(!row)return; var key=row.dataset.categoryKey; var category=state.categories.find(function(item){return item.key===key;});
            try {
                if(event.target.closest("[data-category-rename]")){var name=window.prompt("Новое название раздела",category.name);if(name===null)return;await api("/api/service-categories/"+encodeURIComponent(key),{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:name})});await reloadCategories();notify("Раздел переименован");}
                else if(event.target.closest("[data-category-delete]")){pendingCategoryDelete=category;var wrap=categoryDeleteDialog.querySelector("[data-category-replacement-wrap]");wrap.hidden=!category.service_count;categoryDeleteDialog.querySelector("[data-category-delete-description]").textContent=category.service_count?"В разделе есть сервисы. Перед удалением выберите, куда их перенести.":"Раздел пуст и будет удалён без переноса сервисов.";var select=categoryDeleteDialog.querySelector("select");select.innerHTML=state.categories.filter(function(item){return item.key!==key;}).map(function(item){return '<option value="'+escapeHtml(item.key)+'">'+escapeHtml(item.name)+'</option>';}).join("");categoryDeleteDialog.querySelector(".form-status").textContent="";categoryDeleteDialog.showModal();}
                else if(event.target.closest("[data-category-move]")){var index=state.categories.findIndex(function(item){return item.key===key;});var target=event.target.closest("[data-category-move]").dataset.categoryMove==="up"?index-1:index+1;if(target<0||target>=state.categories.length)return;var moved=state.categories.splice(index,1)[0];state.categories.splice(target,0,moved);renderCategoryManager();await api("/api/service-categories/reorder",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ordered_keys:state.categories.map(function(item){return item.key;})})});renderCategoryControls();}
            } catch(error){categoryDialog.querySelector("[data-category-status]").textContent=error.message;await reloadCategories().catch(function(){});}
        });
        categoryDeleteDialog.querySelectorAll("[data-category-delete-close]").forEach(function(button){button.addEventListener("click",function(){categoryDeleteDialog.close();});});
        document.getElementById("categoryDeleteForm").addEventListener("submit",async function(event){event.preventDefault();if(!pendingCategoryDelete)return;var status=categoryDeleteDialog.querySelector(".form-status");try{var replacement=pendingCategoryDelete.service_count?this.elements.replacement_key.value:"";await api("/api/service-categories/"+encodeURIComponent(pendingCategoryDelete.key),{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({replacement_key:replacement})});categoryDeleteDialog.close();pendingCategoryDelete=null;await refresh();renderCategoryManager();notify("Раздел удалён, сервисы сохранены");}catch(error){status.textContent=error.message;}});
    }
    var deleteDialog = document.getElementById("serviceDeleteDialog"), deleteForm = document.getElementById("serviceDeleteForm"), pendingDelete = null;
    function openDeleteDialog(id) {
        if (!deleteDialog || !boot.isOwner) return;
        pendingDelete = state.services.find(function(item) { return item.id === id; });
        if (!pendingDelete || !pendingDelete.archived) return;
        deleteForm.reset();
        deleteDialog.querySelector("[data-delete-name]").textContent = pendingDelete.name;
        deleteDialog.querySelector("[data-delete-required]").textContent = pendingDelete.name;
        deleteDialog.querySelector("[data-delete-submit]").disabled = true;
        deleteDialog.querySelector(".form-status").textContent = "";
        deleteDialog.showModal();
    }
    if (deleteDialog) {
        var deleteConfirm = deleteForm.elements.confirm_name;
        deleteConfirm.addEventListener("input", function() {
            deleteDialog.querySelector("[data-delete-submit]").disabled = !pendingDelete || this.value.trim() !== pendingDelete.name;
        });
        deleteDialog.querySelectorAll("[data-delete-close]").forEach(function(button) { button.addEventListener("click", function() { deleteDialog.close(); }); });
        deleteForm.addEventListener("submit", async function(event) {
            event.preventDefault();
            if (!pendingDelete || deleteConfirm.value.trim() !== pendingDelete.name) return;
            var status = deleteDialog.querySelector(".form-status");
            try {
                await api("/api/services/" + pendingDelete.id, {method:"DELETE"});
                deleteDialog.close();
                notify("Сервис «" + pendingDelete.name + "» удалён навсегда");
                pendingDelete = null;
                await refresh();
            } catch (error) { status.textContent = error.message; }
        });
    }
    function addAccountField(account) {
        account = account || {};
        var node = document.createElement("div"); node.className = "account-form"; node.dataset.id = account.id || "";
        node.innerHTML = '<label>Название аккаунта<input data-account="label" value="' + escapeHtml(account.label || "Основной аккаунт") + '" maxlength="120"></label><label>Логин<input data-account="login" autocomplete="off" placeholder="' + (account.id ? "Оставьте пустым без изменений" : "") + '"></label><label>Пароль<input data-account="password" type="password" autocomplete="new-password" placeholder="' + (account.id ? "Оставьте пустым без изменений" : "") + '"></label><button type="button" class="account-remove">Удалить</button>';
        node.querySelector(".account-remove").addEventListener("click", function(){node.remove();}); accountFields.appendChild(node);
    }
    function buildPermissions(service) {
        permissionFields.innerHTML = "";
        var canManage = !service || service.permissions.can_manage_access;
        document.getElementById("permissionsSection").hidden = !canManage;
        if (!canManage) return;
        (boot.users || []).filter(function(user){return user.system_role !== "admin" && user.role !== "owner";}).forEach(function(user){
            var grant = service && (service.grants || []).find(function(item){return item.user_id === user.id;}) || {};
            var row=document.createElement("div"); row.className="permission-user"; row.dataset.userId=user.id;
            row.innerHTML='<strong>'+escapeHtml(user.display_name)+'</strong><div class="permission-options">'+[["can_view","Видеть"],["can_open","Открывать"],["can_view_login","Видеть логин"],["can_copy_login","Копировать логин"],["can_view_password","Видеть пароль"],["can_copy_password","Копировать пароль"],["can_edit","Редактировать"],["can_manage_access","Управлять доступами"],["can_archive","Архивировать"]].map(function(pair){return '<label><input type="checkbox" data-permission="'+pair[0]+'" '+(grant[pair[0]]?'checked':'')+'> '+pair[1]+'</label>';}).join("")+'</div>'; permissionFields.appendChild(row);
            row.addEventListener("change", function(event) {
                var changed = event.target.closest("[data-permission]");
                if (!changed) return;
                var view = row.querySelector('[data-permission="can_view"]');
                if (changed === view && !view.checked) {
                    row.querySelectorAll("[data-permission]").forEach(function(input) { input.checked = false; });
                } else if (changed !== view && changed.checked) {
                    view.checked = true;
                }
            });
        });
        if (!permissionFields.children.length) permissionFields.innerHTML='<span class="field-hint">Других пользователей пока нет.</span>';
    }
    function setIconFormState(hasIcon) {
        var preview = form.querySelector("[data-icon-preview]"), empty = form.querySelector("[data-icon-empty]"), upload = form.querySelector("[data-icon-upload]"), replace = form.querySelector("[data-icon-replace]"), remove = form.querySelector("[data-icon-remove]");
        preview.hidden = !hasIcon;
        empty.hidden = hasIcon;
        upload.hidden = hasIcon;
        replace.hidden = !hasIcon;
        remove.hidden = !hasIcon;
    }
    function openForm(id) {
        var service = id ? state.services.find(function(item){return item.id===id;}) : null;
        form.reset(); accountFields.innerHTML=""; document.getElementById("serviceId").value=service?service.id:""; document.getElementById("serviceVersion").value=service?service.version:"";
        document.getElementById("serviceDialogTitle").textContent=service?"Изменить сервис":"Добавить сервис";
        renderCategoryControls();
        var iconPreviewImage = form.querySelector("[data-icon-preview-image]"), iconFile = form.elements.icon_file;
        iconPreviewImage.removeAttribute("src"); iconFile.value = ""; form.elements.icon_remove.value = "0";
        if(service){form.elements.name.value=service.name;form.elements.url.value=service.url;form.elements.description.value=service.description;form.elements.category.value=service.category;form.elements.favorite.checked=service.favorite;service.accounts.forEach(addAccountField);if(service.has_custom_icon){iconPreviewImage.src=iconUrl(service);}}else addAccountField();
        setIconFormState(Boolean(service && service.has_custom_icon));
        buildPermissions(service); document.getElementById("serviceFormStatus").textContent=""; dialog.showModal();
    }
    if (dialog) {
        document.getElementById("addService").addEventListener("click",function(){openForm();}); document.getElementById("addAccount").addEventListener("click",function(){addAccountField();});
        dialog.querySelectorAll("[data-close]").forEach(function(button){button.addEventListener("click",function(){dialog.close();});});
        form.addEventListener("submit",async function(event){event.preventDefault();var status=document.getElementById("serviceFormStatus");status.textContent="";
            var accounts=Array.from(accountFields.children).map(function(row){return {id:Number(row.dataset.id||0),label:row.querySelector('[data-account="label"]').value,login:row.querySelector('[data-account="login"]').value,password:row.querySelector('[data-account="password"]').value};});
            var permissions=document.getElementById("permissionsSection").hidden?null:Array.from(permissionFields.querySelectorAll(".permission-user")).map(function(row){var result={user_id:Number(row.dataset.userId)};row.querySelectorAll("[data-permission]").forEach(function(input){result[input.dataset.permission]=input.checked;});return result;});
            var payload={name:form.elements.name.value,url:form.elements.url.value,description:form.elements.description.value,category:form.elements.category.value,favorite:form.elements.favorite.checked,version:Number(document.getElementById("serviceVersion").value||0),accounts:accounts,permissions:permissions};
            var file=form.elements.icon_file.files[0], id=document.getElementById("serviceId").value; payload.icon_remove=form.elements.icon_remove.value === "1" && !file;
            var body=new FormData();body.append("payload",JSON.stringify(payload));if(file)body.append("icon",file);
            try{await api(id?"/api/services/"+id:"/api/services",{method:id?"PUT":"POST",body:body});dialog.close();notify(id?'Сервис «'+payload.name+'» сохранён':'Сервис «'+payload.name+'» добавлен');await refresh();}catch(error){status.textContent=error.message;}
        });
        form.querySelectorAll("[data-icon-upload],[data-icon-replace]").forEach(function(button){button.addEventListener("click",function(){form.elements.icon_file.click();});});
        form.querySelector("[data-icon-remove]").addEventListener("click",function(){form.elements.icon_file.value="";form.elements.icon_remove.value="1";form.querySelector("[data-icon-preview-image]").removeAttribute("src");setIconFormState(false);});
        form.elements.icon_file.addEventListener("change", function() {
            var file = this.files && this.files[0], preview = form.querySelector("[data-icon-preview]"), image = form.querySelector("[data-icon-preview-image]"), status = document.getElementById("serviceFormStatus");
            if (!file) return;
            if (file.size > 512 * 1024 || !["image/png","image/jpeg","image/webp"].includes(file.type)) { this.value=""; status.textContent="Поддерживаются PNG, JPEG и WEBP размером до 512 КБ"; return; }
            form.elements.icon_remove.value="0"; status.textContent="";
            var reader = new FileReader();
            reader.onload = function() { image.src = reader.result; setIconFormState(true); };
            reader.readAsDataURL(file);
        });
    }
    var archiveToggle=document.getElementById("archiveToggle"); if(archiveToggle)archiveToggle.addEventListener("click",async function(){state.archived=!state.archived;state.filter="all";state.query="";document.getElementById("serviceSearch").value="";this.textContent=state.archived?"Вернуться к сервисам":"Показать архив";try{await refresh();}catch(error){notify(error.message,true);}});
    renderCategoryControls();
    render();
}());
