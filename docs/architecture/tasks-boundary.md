# Граница задач и операционных разделов ERP

Статус: current implementation в `codex/tasks-legacy-retirement`, 2026-09-27.
Документ сам по себе не подтверждает production deploy.

## Один модуль задач

Единственный пункт меню «Задачи» использует прежний navigation key `tasks`,
но ведёт на `/app/tasks-module`. Это сохраняет порядок и персональное скрытие
раздела. В desktop и mobile нет второго пункта «Новые задачи».
Новый badge загружается после window.load при включённом и зарегистрированном
модуле. Ошибка badge не препятствует рендеру ERP.

Legacy TaskStore, типы ошибок, template, CSS/JS, CRUD/calendar/repeat handlers,
генерация старых task notifications и task assignment branch удалены из приложения.
`/app/tasks` возвращает 302 на корень нового модуля без любых query parameters.
GET/POST/PUT/PATCH/DELETE `/api/v1/tasks` и подмаршрутов возвращают JSON 410
`LEGACY_TASKS_RETIRED`, no-store. Авторизация ERP остаётся обязательной;
anonymous API получает 401. Новый namespace `/api/v1/tasks-module` независим.
Старые IDs никогда не интерпретируются как IDs новых задач.
При feature flag OFF старый модуль не возвращается; новый остаётся недоступен.

## Сохранность данных

`tasks.db` НЕ удаляется: в ней продолжают жить `entity_assignments`,
`assignment_history`, `inbox_events` для orders/customers/purchases/repairs.
Их schema validator и транзакции с общим ERP audit сохранены.
Исторические migrations, recovery contract и все legacy таблицы/строки
оставлены без изменений. Старые записи не переносятся в новую базу и не стираются.

Исторические `entity_type=task` события исключены из общих Inbox list/count/badge
и операций отметки прочтения. Аналогично `type=task` исключён из общей ленты
уведомлений auth.db и её unread/read операций; новые события такого типа не создаются.
Общие заказы и системные уведомления работают по прежнему контракту.
Поле preferences `task_sound` сохранено для совместимости схемы, но старый UI
настройки и обработка старых task alerts удалены. Исторический журнал остаётся,
его task records больше не содержат активной ссылки на старый экран.

## Новый Tasks core

`app/tasks/` работает только с `tasks-module.db`; бизнес-код не открывает
legacy Tasks, catalog или складские базы. Auth adapters читают пользователей
read-only. Основные ERP страницы не открывают оба Tasks storage при рендере.
Миграции нового модуля выполняются явно; retirement не меняет схемы.

Одноразовый `ops/tasks-release-classification.json` относился только к предыдущему
выпуску и удалён. Штатные schema/deploy/recovery gates продолжают действовать.
Rollback к предыдущему commit не требует восстановления или downgrade БД;
он вернёт старый интерфейс, поэтому используется только как аварийный откат кода.

## Проверки

- `test_tasks_api.py`: retired routes, auth, отсутствие Tasks DB access, старые
  ссылки, единое desktop/mobile меню, preferences, новый CRUD namespace, flag OFF.
- `test_collaboration*.py`: назначения ERP, атомарность audit/history/inbox,
  запрет новых legacy task assignments и сохранность исторических событий.
- `test_user_notifications.py`: ERP order/system delivery, дедупликация,
  личное прочтение; legacy task records сохранены, но не выдаются.
- `test_tasks_isolation.py`: ошибки/отсутствие/блокировки обеих Tasks DB не
  влияют на основные ERP страницы; shared assignments работают без task tables.
- `frontend/e2e/tasks.spec.ts`: единое меню, legacy redirect, новый CRUD/archive,
  микрозадачи, отсутствие старых API requests и горизонтального overflow.
- `scripts/validate_tasks_runtime.py`: exact Linux/Python 3.6.8/SQLite 3.7.17
  на временных данных, без сети и доступа к production БД.

Переключённый accessibility gate нового экрана выявил недостаточный контраст
инициалов `.tm-avatar`; цвет текста заменён на существующий `--erp-text`.
Макет, схема и бизнес-логика нового модуля не менялись.

## Проверка этого изменения

- Windows Python 3.12: relevant backend suite 264 tests, 261 PASS, 3 POSIX skips;
  после уточнения Location для Flask 2.0 ещё 19 targeted checks PASS.
- Linux CentOS 7.9 / Python 3.6.8 / SQLite 3.7.17 / Flask и Werkzeug 2.0.3:
  349/349 PASS, 0 skips. UID 99, network namespace отключён, 7019 synthetic
  SQLite connections, 0 обращений к БД вне временных fixtures. 50 source hashes
  совпадают с нормализованными LF исходниками Git; evidence:
  `docs/validation/tasks-retirement-runtime.json`.
- POSIX IMMEDIATE/EXCLUSIVE locks обеих Tasks DB: Orders/Products 200,
  без открытий Tasks storage. Runtime DDL gate PASS; схемы не менялись.
- JS adapters/state/badges: 29 checks PASS; event notifications: 3 tests PASS.
- Новый экран: Axe (без serious/critical), focus trap / Escape / возврат фокуса PASS.

Полный CI и production smoke фиксируются отдельно при публикации и deploy.
