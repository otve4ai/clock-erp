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

## Stage F: измеренная базовая линия (2026-09-27)

Статус: LOW, измерено; оптимизация отложена по результатам synthetic acceptance.
`scripts/benchmark_tasks_module.py`, evidence `docs/validation/tasks-final-f-performance.json`.
Linux/Python3.6.8/SQLite3.7.17, 30000 normal +2000 micro, 500 Projects, 100 users,
10000 Activity одного Task, 2000 Inbox events, 7 samples после warm-up.
Status не коррелирует с user_id; deep pages обязаны быть непустыми.

Медианы: my101.6ms, all105.3ms, search106.2ms, overdue81.3ms,
archive53.5ms, history offset9000 24.6ms, Projects30+counters5.4ms,
Projects100+counters12.1ms, admin offset15000 162.2ms (max174.1).
20 записей с параллельным reader: 0 busy/unexpected errors, median write4.7ms,
max114.9ms. Это лёгкая synthetic concurrency, не нагрузочное доказательство SLA.

EXPLAIN подтверждает TEMP B-TREE обычных списков/Activity/Inbox; старый planner
нередко выбирает tasks_micro_deadline по task_type/deleted_at даже для normal,
а today/overdue boolean сохраняет COALESCE. Archive использует tasks_completed;
Project page aggregate использует covering tasks_project_active, без N+1 COUNT.
33 различных SQL plans сохранены. Новых индексов/SQL rewrite/ANALYZE в HTTP нет.

В этом профиле измеренный максимум174ms не оправдывает расширение acceptance
рефакторингом SQL. Повторить измерения на ожидаемом реальном объёме/распределении
и concurrency перед масштабированием; затем сравнить минимальную оптимизацию.
