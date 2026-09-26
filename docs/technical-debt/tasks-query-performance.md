# TD-TASKS-001 — планы запросов Tasks перед performance acceptance

Статус: `deferred`, приоритет LOW. Источник: независимое review этапа B;
отсрочка прямо согласована владельцем в B.1. Не является scope этапа C автоматически.

Наблюдения:
- ORDER BY deadline_date IS NULL, deadline_date, id требует дополнительной сортировки.
- Индекс истории (task_id, task_version, id) не соответствует ORDER BY id.
- COALESCE вокруг положительных today/overdue мешает прямому ограничению по дате.
- Substring search LIKE %...% просматривает доступный пользователю набор.

До изменения SQL или индексов измерить фактическое время и EXPLAIN QUERY PLAN
на SQLite 3.7.17: тысячи задач, длинная история, первые/глубокие страницы,
my/created/all, today/overdue/search, конкурентное чтение и запись. Фиксировать
объём и распределение данных, время запросов и ожидания блокировок.

После измерений согласовать минимальную оптимизацию и сравнить до/после.
Сохранить permissions, counts, NULL deadlines, порядок и pagination.
Не применять expression/partial indexes или другой SQL новее production runtime.

В B.1 запросы и индексы сознательно сохранены без изменений.
