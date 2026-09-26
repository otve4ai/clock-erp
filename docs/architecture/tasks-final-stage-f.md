# Tasks Stage F — final acceptance

Основание: final PASS Stage E, commit `b56e3b061c0f983288781793ce580ad775867852`.
Ветка `codex/tasks-final-stage-f`. Финальный независимый review и delta-review: PASS.
Итог A–F: PASS, подтверждённых открытых blockers нет. Последний LOW правых Project
counters закрыт, проверен unit/browser и принят финальным delta-review.
Review: [«Задачи по ERP»](https://chatgpt.com/g/g-p-6aa80e13ea2881919639c9b041fc3174-erp-bitriks/c/6ab7c9f4-ef24-83ed-b0ca-ff67bd5f9b0d).
Согласованный цикл D → E → F завершает разработку, но не включает rollout.
Feature flag OFF; push, merge, deploy, production enable не выполняются.

## Изменения F

Новых функций, схемы, migrations, прав и business transactions нет.

- `app/static/js/tasks-module.js`: Archive period chips соответствуют ручным
  датам/reset; notice очищается при navigation; micro overdue корректно показывает
  первые секунды/минуты; recovery micro preview очищает старую ошибку, stale failure
  не перекрывает свежий успех; при ошибке вместо вечного loading — понятное состояние.
  После быстрой mutation проектной Task обновляются и правые Project counters.
- `tests/test_tasks_ui_state_js.js`: прежние четыре race regressions плюс preset/
  clock/notice, recovery/out-of-order failure и Project counters после checkbox.
  Неизменённый production controller
  исполняется в VM; DOM и transport управляются fixture.
- `tests/test_tasks_acceptance.py`: четыре security/isolation tests через настоящие
  routes/services/SQLite, без production data.
- `scripts/benchmark_tasks_module.py`: воспроизводимые synthetic measurements и
  EXPLAIN QUERY PLAN с защитой от открытия посторонних БД/сети.
- `scripts/validate_tasks_runtime.py`: включает F tests и compile/source hashes.
- `docs/validation/tasks-final-f-runtime.json`, `tasks-final-f-ddl.json`,
  `tasks-final-f-performance.json`: фактические exact-runtime результаты.
- Этот отчёт, `docs/document-register.md`,
  `docs/technical-debt/tasks-query-performance.md`: evidence и ограничения.

## Backend и runtime

Relevant suite A–F + legacy Tasks/collaboration/navigation/notifications:

| Runtime | Результат |
| --- | --- |
| Linux CentOS 7.9 / Python 3.6.8 / SQLite 3.7.17 / Flask 2.0.3 / Werkzeug 2.0.3 | 261 PASS, 0 skips |
| Windows / Python 3.12.14 / SQLite 3.53.1 | 260 PASS, 1 POSIX-only lock skip |
| Node transport/notification adapters | 15 checks PASS |
| Node controller regressions | 7 checks PASS |
| DDL gate | PASS, 21 migration modules |

40 Python files компилируются actual Python 3.6.8; SHA проверяются против staged
Git blobs. Статические JS проходят node --check; HTML/Jinja проверены настоящим
Flask render и браузером. Новый frontend — статические assets, отдельного bundler нет.
Это полный relevant suite Tasks/затронутых ERP механизмов, не весь исторический ERP CI.

Exact run: временная source-only копия в /tmp, UID99 nobody, `unshare -n`, пустое
окружение, synthetic fixtures, SQLite path guard. Production базы не используются.
Временная копия `/tmp/erp-tasks-f.LVXAzDi0` удалена после получения evidence;
production проверен только read-only: HEAD
`a0e53422c023bfaa8bdc40759b2f4587ea650d1c`, service `clock-erp` active, без изменений.
SMS не менялись и не входят в проверяемую новую функциональность.

## Browser E2E и visual acceptance

На локальном Flask с настоящими API и synthetic DB, Chrome:

- обычная Task: create, edit, status, done, history, soft-delete, restore;
- project create/rename, add/remove member, archive/restore без изменения Tasks;
- Project list и board: четыре статуса, настоящий drag и select alternative;
- micro: quick add, preview4, полный список, collapse/reload, complete/reopen,
  неизменный deadline, convert с сохранением ID;
- Inbox: explicit read; GET200/read503 всё равно открывает drawer, pending остаётся;
- два браузерных окна редактируют одну Task: stale update даёт 409 и refresh,
  без silent overwrite;
- member-only viewer не редактирует; assignee редактирует содержание, но не
  переназначает/удаляет; private ID другому user даёт 404;
- HTML/script в названии/описании остаётся текстом; валидный Unicode отображается;
- actual `VechasuNotify.info`: title payload `<img src=x onerror=alert(1)>`
  виден буквальным текстом, в toast DOM нет img/script. Его renderer использует
  textContent (`app/static/js/notifications.js`), action href задаётся приложением;
- real Tasks storage exception и отдельный JS503 не мешают Orders/Products/sidebar;
- recovery через «Повторить» без reload возвращает 4 micro rows и скрывает ошибку;
- quick checkbox completion: Task исчезает из активного списка, Project preview
  меняется с 4 открытых на 3 без reload;
- classic/night themes, drawer keyboard Tab/Shift+Tab trap, Escape/close, focus;
- loading/error/empty states, сброс preset после ручной даты/«Сбросить»;
- ширина390: фактический content viewport375 со scrollbar, scrollWidth=clientWidth;
  архив превращается в компактные записи с подписями, без horizontal overflow.

Главный экран следует последнему MASTER «Фото 1.jpg»: пять показателей, широкий
горизонтальный блок четырёх микрозадач, compact grouped list, right Projects/people.
Смысл подписей — утверждённый backend: Входящие / Поставленные мной / Ожидаю.
Reference архива недоступен в исходной переписке; используется одобренный structural
fallback, pixel-match не заявляется. Никаких фиктивных HR ролей/чисел сотрудников.

Локальные screenshots вне Git: `qa-reports/tasks-f-screens` во внешнем workspace:
01-main, 02-micro-collapsed, 03-inbox, 04-task-drawer, 05-micro-list, 06-projects,
07-project-list, 08-project-board, 09-archive, 10-mobile-main, 11-mobile-archive,
12-erp-toast, 13-tasks-error, 14-dark-main. Исходный макет: `qa-reports/tasks-master-latest.jpg`.

## Security и изоляция

Новый Tasks CRUD/Projects/Micro/Inbox/claim открывают только tasks-module.db.
Внешний user lookup/directory — узкий read-only ERP adapter, до локальной business
transaction. Legacy tasks.db, catalog/warehouse/Orders/Products не используются.
Обычное сохранение ERP session в auth.db не является Tasks business transaction.

Permission policy единая: creator/admin управляют, assignee редактирует содержание,
Project owner/member дают VIEW, не EDIT. API не доверяет presentation capabilities.
IDOR защищён и для direct ID/history, и для list/search/count/summary. Права повторно
проверяются сервером, SQL параметризован. CSRF rejected до открытия storage на всех
семействах mutations. Mass assignment, invalid Unicode/body/size, schema corruption,
optimistic version, Activity rollback и migration idempotence входят в relevant suite.

Isolation tests A–F подтверждают missing/corrupt/locked Tasks DB, failed import/
registration/migration, flag OFF, safe HTTP exceptions. Orders/Products при обычном
серверном рендере не открывают ни legacy, ни новый Tasks storage. Дополнительные
browser-fault checks подтверждают работу общей навигации при Tasks API/JS failure.

## Performance

Методика, полные timings и 33+ query plans находятся в отдельном JSON evidence.
32000 Tasks (30000 normal + 2000 micro), 500 Projects, 100 синтетических users,
10000 Activity одного Task, 2000 Inbox events. Семь измерений после warm-up.
Непустые first/deep pages проверяются assert; status варьируется независимо от user_id.
Измеряется настоящий service + permissions + schema inspection + connection/transaction.
Двадцать локальных записей параллельно с reader; unexpected thread errors проверяются.

| Сценарий | Медиана, ms |
| --- | ---: |
| my / all | 101.6 / 105.3 |
| search / overdue | 106.2 / 81.3 |
| Archive / Activity offset9000 | 53.5 / 24.6 |
| Projects30+counters / Projects100+counters | 5.4 / 12.1 |
| Admin deep offset15000 | 162.2 (max174.1) |
| Inbox / micro list | 2.3 / 5.7 |
| 20 concurrent writes | 4.7 (max114.9) |

Reader: 5 завершённых запросов, 0 busy, 0 unexpected errors, max181.5ms.

SQL/schema/indexes оставлены прежними: измерения не оправдали расширение F большой
оптимизацией. TD-TASKS-001 получает измеренную базовую линию и остаётся LOW для будущего
роста данных. Temp sort/substring scan/старые planner estimates документированы.
Project list считает counters агрегатом выбранной страницы, без N+1 COUNT.
Синтетический benchmark не является production load test или гарантией SLA.

## Ограничения и будущий rollout

- Active user directory максимум1000, превышение — явная ошибка; selectors проектов
  читают доступные страницы. Для большого штата нужен отдельный paginated selector.
- Browser acceptance в современном Chrome, не во всех браузерах/assistive technologies;
  формальная WCAG сертификация не выполнялась.
- Notification claim at-most-once: потеря ответа после claim может потерять toast,
  но Inbox остаётся durable и непрочитанным до explicit read (принятый D контракт).
- Модуль выключен, отдельный preview entry; legacy не переключён и данные не перенесены.
- Comments/checklist/files/calendar/ERP entity integration не добавлены.
- Перед rollout: отдельное согласование, backup, explicit offline migration новой
  пустой tasks-module.db, smoke на staging, затем отдельное включение флага.
  Никаких lazy migrations/repair из HTTP.

После принятого final review и локального commit Stage F разработка останавливается.
