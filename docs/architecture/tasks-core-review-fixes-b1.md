# B.1 — исправления независимого code review

Статус: `current` для `codex/tasks-core-stage-b1`, 2026-09-26.
Основание: `c5c1762785d0ce0f840da05f37456e0c3004e941`.
Только исправления review и regression tests. Модуль остаётся OFF; UI,
Projects, Microtasks, Inbox, legacy migration, push, merge и deploy не входят в этап.

## Schema validation

Пассивный `app/tasks/schema.py` содержит неизменённый DDL этапа B и ожидаемые
свойства колонок. Его импорт не открывает connections и не запускает миграции.
Offline migration и read-only validator используют один DDL-контракт.
Версия схемы остаётся 2; таблицы и индексы не изменяются, новая миграция не нужна.

`validate_connection()` проверяет все колонки `tasks`, `task_activity` и
`tasks_module_migrations`: имя, declared type, NOT NULL, DEFAULT, позицию PK.
Используются обычные PRAGMA table_info / foreign_key_list и sqlite_master,
подтверждённые на SQLite 3.7.17. FK проверяется полностью, включая действия
ON UPDATE / ON DELETE и MATCH.

Весь CREATE TABLE сравнивается по токенам с каноническим DDL: это проверяет
CHECK и их логическое окружение, а не только присутствие текста ограничения.
Пробельное форматирование и регистр SQL-слов несущественны; строковые литералы
сравниваются точно. Иные, даже семантически эквивалентные переписывания DDL
могут быть отклонены: принимается схема, созданная нашим offline runner.
Это намеренный fail-closed контракт, не универсальный SQL parser.

Обнаруживаются удаление NOT NULL, смена типов/defaults/PK/FK, удаление или
ослабление CHECK, нарушение связи status/completed_at, ограничения deadline,
activity и migration ledger. Сохраняются проверки версии, индексов и отсутствия
неожиданных таблиц/views/triggers. Repair и DDL в HTTP отсутствуют.
При несовпадении Tasks возвращает локальный 503; байты базы не изменяются,
основные ERP routes продолжают работать.

## HTTP и Unicode

HTTPException обрабатывается до общего Exception: исходный HTTP status
сохраняется, ответ имеет `code=HTTP_ERROR`, безопасное общее сообщение и
Cache-Control: no-store. Description, traceback, SQL и внутренние пути наружу
не передаются. Проверены 400/401/403/404/413. TaskError сохраняет прежние
422/403/404/409; настоящие storage/internal failures остаются локальным
503 TASKS_MODULE_UNAVAILABLE.

Malformed JSON, оборванное тело и отсутствие JSON content type дают 400.
Синтаксически корректный JSON неподходящей структуры (массив/null/строка)
по-прежнему даёт 422.

Установленный Werkzeug 2.0.3 не ограничивает JSON stream через
MAX_CONTENT_LENGTH автоматически. При заданном лимите Tasks payload reader
проверяет Content-Length и читает не более limit+1 bytes, включая WSGI stream
без Content-Length. Превышение даёт 413 до обращения к storage. Глобальный
лимит не вводится и общий request/session middleware не меняется.

Общий text_value выполняет строгий encode('utf-8'). Surrogate отклоняется
как 422 до SQLite binding. Это распространяется на title, description,
related_entity_* и search. Кириллица, японский, арабский, emoji и combining
characters сохраняются без потерь и без принудительной нормализации.

## Regression tests и runtime

Добавлены 24 test methods: 9 schema, 6 core, 8 API и 1 реальная ERP isolation.
Subtests дополнительно покрывают множество колонок, ограничений и Unicode.
Проверены mixed PATCH без частичной записи, rollback reassign вместе с уже
добавленным событием content_changed, московская полночь 20:59:59Z/21:00:00Z,
повторные delete/restore (stale version 409, повтор в текущем состоянии 403,
без лишней version/history).

Финальный обязательный suite A/A.1/B/B.1: **158 passed, 0 failures, 0 errors,
0 skipped**, 21.887 s. Настоящий Linux CentOS 7.9 / Python 3.6.8 / SQLite 3.7.17,
Flask и Werkzeug 2.0.3. 28 Python-файлов скомпилированы именно Python 3.6.8.
SHA256 исходников сверены с локальным кандидатом (LF/Git representation).
Локальный финальный suite: 158 tests, 0 failures/errors, 1 skip (Linux-only),
26.440 s, Windows / Python 3.12.14 / SQLite 3.53.1 / Werkzeug 3.1.8.
Проверены как превышение body limit без Content-Length, так и точное равенство лимиту.

Проверка проведена после отдельного разрешения владельца в одноразовом
`/tmp/erp-tasks-b1.8IWqUzlR/source`: только tracked source и B.1 overlay,
без .env, рабочих БД и пользовательских файлов. DB guard зарегистрировал
3790 connections и 0 попыток открыть путь вне fixtures. 30 ATTACH относятся
к legacy regression fixtures и отрицательному тесту запрета ATTACH, не к новому CRUD.
Запуск от nobody (UID 99),
env -i, network namespace без сети, Python network guard и DB path guard.
Установленный venv только использован; пакеты и production-код не изменялись.
После сохранения результатов временная серверная копия удалена. Сервис остался
active, production HEAD — a0e53422c023bfaa8bdc40759b2f4587ea650d1c, без изменений.
Первый прогон с ASCII locale дал одну ошибку старого теста имени файла с
кириллицей; окончательный прогон выполнен с en_US.UTF-8, без изменения теста.

Linux lock tests: Orders+Products при IMMEDIATE — 0.130 s, при EXCLUSIVE —
0.135 s; попыток открыть legacy/new Tasks storage — 0. DDL gate: passed,
21 controlled modules, 0 ensure functions. SQL gate: 21 source files.

Машинные результаты:
- [Exact runtime](../validation/tasks-core-b1-runtime.json)
- [DDL gate](../validation/tasks-core-b1-ddl.json)

CRUD guard подтверждает отсутствие legacy/catalog/auth connections из core;
реальный identity adapter проверен с mode=ro и запретом DML. Общая ERP session
может сохраняться в auth.db — это согласованная инфраструктура, не Task
business transaction. Feature OFF, отсутствие миграций в request path и
отказы Tasks при доступных Orders/Products проверены повторно.

## Полный backend suite и проверка baseline

Расширенный локальный запуск всех `test*.py`: **2101 tests, 14 failures,
56 errors, 15 skipped**, 1665.554 s. Этот Windows-прогон не считается зелёным
CI и не заменяет полный Linux CI. Использован локальный Windows adapter с
фиктивным fcntl; он не доказывает POSIX locking/permissions.

Все 63 ошибки/failures вне Tasks воспроизведены отдельными запусками на
неизменённом базовом commit c5c1762785d0ce0f840da05f37456e0c3004e941.
Сверены все заголовки случаев, включая parametrized subtests; неподтверждённых
случаев вне Tasks нет. Причины включают Windows POSIX permissions/flock,
недоступные shell-команды и cp1251 decoding в существующих тестах.
Продуктовый код этих подсистем не исправлялся.

Остальные 7 errors возникали при повторном Flask startup в Tasks tests:
предшествующий test_sms оставляет ERP_SMS_DATABASE на удалённую shared fixture,
и SmsStore.verify падает до проверки Tasks. Интеграционным тестам Tasks выданы
собственные временные SMS fixtures; приложение и SMS middleware не изменялись.
После исправления порядок `test_sms.py -> test_tasks_core*.py ->
test_tasks_isolation.py` проходит: **101 tests, 0 errors/failures, 1 Linux-only
skip**, 23.337 s. Затем повторены финальные обязательные 158 тестов на Windows
и Linux с результатами выше.

Полные 2101 тест начаты до окончательной корректировки bounded JSON reader
и тестовых fixtures; весь длительный ERP suite после них повторно не запускался.
Все затронутые группы повторены целиком. Поэтому общий ERP suite здесь не
объявляется полностью успешным; для будущей публикации требуется штатный CI.

Локальные исходные логи в workspace `qa-reports/`:
`tasks-b1-full-backend.log`, `tasks-b1-baseline-known-failures.log`,
`tasks-b1-baseline-extra*.log`, `tasks-b1-baseline-final.log`,
`tasks-b1-sms-order-regression.log`, `tasks-b1-local-final.log`,
`tasks-b1-runtime.log`. Ни один тест не удалён/выключен ради успешного результата.

## Отложенная работа

[TD-TASKS-001: query performance](../technical-debt/tasks-query-performance.md)
отложен до performance acceptance. Business SQL list/summary/activity,
COALESCE, ORDER BY и определения индексов в B.1 не менялись.

## Изменённые файлы

| Файл | Изменение |
| --- | --- |
| `app/tasks/schema.py` | Пассивный DDL и контракт колонок/FK; сравнение SQL-токенов |
| `app/tasks/repository.py` | Полная read-only проверка таблиц и ограничений |
| `app/tasks/migrations.py` | Использование общего неизменённого DDL; без новой версии/миграции |
| `app/tasks/error_boundary.py` | Безопасный JSON с исходным HTTPException status |
| `app/tasks/routes.py` | Строгий JSON parse и ограничение тела для старого/нового Werkzeug |
| `app/tasks/domain.py` | Общая строгая UTF-8 validation |
| `app/schema_migrations.py` | Только добавление schema.py в offline SQL compatibility scan |
| `scripts/validate_tasks_runtime.py` | B.1 base и новые файлы в compiler/hash evidence |
| `tests/test_tasks_core_schema.py` | Schema drift и отсутствие repair |
| `tests/test_tasks_core.py` | Mixed PATCH, reassign rollback, полночь, повторы, Unicode |
| `tests/test_tasks_core_api.py` | HTTP statuses, body limits, Unicode, atomic PATCH и собственная SMS fixture |
| `tests/test_tasks_isolation.py` | Реальная ERP при schema/HTTP failures; устранение зависимости теста от чужой SMS fixture |
| `docs/runtime-ddl-inventory.json` | Контроль нового пассивного DDL и актуальные hashes |
| `docs/architecture/tasks-core-review-fixes-b1.md` | Этот отчёт |
| `docs/architecture/tasks-core-stage-b.md` | Ссылка на уточнения B.1 |
| `docs/document-register.md` | Регистрация B.1 и отдельного technical debt |
| `docs/technical-debt/tasks-query-performance.md` | TD-TASKS-001, отложенная performance acceptance |
| `docs/validation/tasks-core-b1-runtime.json` | Фактические результаты Linux, версии и 28 source hashes |
| `docs/validation/tasks-core-b1-ddl.json` | Результат DDL gate на Linux/Python 3.6.8 |

Следующий функциональный этап не начат. После отчёта и локального commit — стоп.
