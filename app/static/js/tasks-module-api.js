(() => {
    "use strict";
    const boot = window.TASKS_MODULE_BOOTSTRAP;
    if (!boot) return;
    const endpoint = "/api/v1/tasks-module";
    async function request(path, method = "GET", values) {
        const controller = new AbortController();
        const timer = window.setTimeout(() => controller.abort(), 10000);
        try {
            const response = await fetch(endpoint + path, {
                method, credentials: "same-origin", signal: controller.signal,
                headers: {"Content-Type": "application/json", "X-CSRF-Token": boot.csrf, "X-Vechasu-Notify": "off"},
                body: values === undefined ? undefined : JSON.stringify(values),
            });
            let payload;
            try { payload = await response.json(); } catch (_) { payload = {}; }
            if (!response.ok) {
                const error = new Error(response.status === 409 ? "Задача уже была изменена. Данные обновлены."
                    : response.status === 403 ? "Недостаточно прав для этого действия."
                    : response.status === 404 ? "Запись не найдена или больше недоступна."
                    : response.status === 422 ? "Проверьте заполненные поля."
                    : "Модуль задач временно недоступен. Попробуйте ещё раз.");
                error.status = response.status;
                error.fields = response.status === 422 && payload.fields ? payload.fields : {};
                throw error;
            }
            return payload.data;
        } catch (error) {
            if (error.name === "AbortError") throw new Error("Сервер не ответил вовремя. Повторите запрос.");
            throw error;
        } finally { window.clearTimeout(timer); }
    }
    window.TasksModuleAPI = Object.freeze({request, query: values => {
        const query = new URLSearchParams();
        Object.entries(values).forEach(([key, value]) => { if (value !== "" && value !== null && value !== undefined) query.set(key, value); });
        return query.toString() ? "?" + query : "";
    }});
})();
