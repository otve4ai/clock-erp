# A.1 — проверка фактического production runtime

Статус: `current`, 2026-09-26. Ветка `codex/tasks-runtime-a1` продолжает
commit A `a806ef287792524e730ff2074d3137eb7aee8664`.
Функциональность нового Tasks не расширена. Merge, push, deploy и переключение
модуля не выполнялись. Рабочий код из `app/` менять не потребовалось.

## Где и как проверено

Удалось использовать установленный на сервере интерпретатор
`/opt/clock-erp/venv/bin/python`, без импорта рабочего экземпляра ERP:

| Компонент | Фактическое значение |
| --- | --- |
| ОС | CentOS 7.9.2009, Linux 3.10.0-1160.25.1.el7.x86_64 |
| Python | 3.6.8 |
| SQLite | 3.7.17 |
| SQLite source ID | `2013-05-20 00:56:22 118a3b35693b134d56ebd780123b7fd6f1497668` |
| Flask / Werkzeug | 2.0.3 / 2.0.3 |
| Jinja | 3.0.3 |

Код Git экспортирован в отдельную копию под `/tmp`, без `.env`, production БД
и пользовательских файлов. Процесс запущен от `nobody` (UID 99), с пустым
окружением и отдельным network namespace `unshare -n`. Внешние интеграции
также отключены штатным backend runner. Установки пакетов и изменения venv нет.

Все подключения SQLite ограничены временным каталогом fixtures; legacy
`ATTACH` проверяет путь к синтетическому каталогу. Операции создания, повреждения,
удаления таблиц и удержания блокировок касаются только этих disposable БД.
Ни одна рабочая production БД не открывалась и намеренно не блокировалась.
Дочерние процессы проверки locks и offline CLI получают только явные пути
к собственным fixtures и наследуют непривилегированного пользователя/отсутствие сети.

В контрольном прогоне: 3131 проверенное подключение SQLite, 29 legacy attachments
к синтетическому каталогу, 0 попыток открыть путь вне fixtures. Эти attachments
относятся к сохранённому collaboration, а не к новому Tasks repository.

Read-only проверки рабочего сервиса показали `active`; указатель release остался
на `a0e53422c023bfaa8bdc40759b2f4587ea650d1c`. Сервис не перезапускался.
После сохранения отчётов временные исходники и fixtures на сервере удалены.

## Найденные несовместимости и исправления

В production-коде diff A несовместимостей с проверенной связкой не обнаружено.
В ходе подготовки и запуска тестов выявлены следующие особенности:

1. Существующий `tests/test_tasks.py` передавал `statements.append` в
   `sqlite3.Connection.set_trace_callback`. В Python 3.6.8 это даёт
   `TypeError: unhashable type: 'list'`. Исправлено на обычную функцию `lambda`,
   сохранив тот же trace и проверку отсутствия DDL. Это реальная несовместимость
   теста; прикладной код Tasks такой callback не использует.
2. SQLite 3.7.17 вызывает authorizer до подстановки параметра в
   `ATTACH DATABASE ? AS ...`: аргумент имени файла равен `None`. Новый защитный
   runner первоначально отвергал допустимые legacy-транзакции. Теперь путь
   проверяется до выполнения SQL по связанному параметру; неизвестный путь
   по-прежнему не разрешается. Сам legacy `ATTACH` не изменён.
3. Первый вариант guard удерживал connection через замыкание authorizer.
   В длинном прогоне это исчерпало файловые дескрипторы. Использован `weakref`,
   чтобы проверочный инструмент не менял lifetime соединений. Это дефект
   промежуточного runner, а не найденная утечка нового Tasks storage.
4. Экспорт Git с Windows `core.autocrlf=true` создавал CRLF-файлы и ложные ошибки
   checksum миграций. Финальный экспорт сделан с `core.autocrlf=false`; контрольные
   суммы соответствуют Git blobs и Linux-контракту. Checksums не подгонялись.

Первоначальные неуспешные прогоны не считаются подтверждением совместимости.
Ни один тест не удалён и не выключен ради итогового результата.

## Проверка всего diff A

Проверены все 28 изменённых файлов A: 19 Python-файлов и 9 файлов иных типов.
Все 19 Python-файлов действительно скомпилированы Python 3.6.8; runtime-тесты
использовали настоящий Flask и SQLite. Дополнительно скомпилированы 3 файла
проверок A.1 — всего 22. Это не проверка `feature_version` современного Python.

| Область | Проверка и вывод |
| --- | --- |
| f-strings, unpacking, исключения | Полные файлы проходят compiler 3.6.8; синтаксис более новых Python не введён |
| pathlib | `resolve()`, `as_uri()`, `mkdir(parents=True, exist_ok=True)`, работа с отсутствующим файлом и экранированием выполнены на 3.6.8 |
| typing / dataclasses | Новых typing API, PEP 585 generics, dataclasses или зависимости от backport нет |
| contextlib / context managers | Новых contextlib API нет; транзакции и `try/finally` выполнены на 3.6.8 |
| Стандартная библиотека | `importlib`, `logging`, `datetime/timezone`, `time.monotonic`, `functools.wraps`, `os` выполняются на фактическом интерпретаторе |
| sqlite3 API | Реально вызваны `uri=True`, `timeout`, `set_progress_handler`, row factory, authorizer, execute/fetch/commit/rollback/close |
| Flask API | Blueprint, route registration, jsonify, current_app, extensions, auth callback и error boundary проверены с Flask 2.0.3 |
| Jinja/sidebar | Настоящие ERP-шаблоны рендерятся Jinja 3.0.3; отдельный badge не нужен для HTML |
| JS/CSS и Node-тест | Не зависят от Python/SQLite сервера; их содержимое A не изменено, прежние 7 Node-тестов сохраняются |
| CI YAML и документация | Не входят в Python request path; новая миграция остаётся в offline inventory |

SHA-256 проверенных файлов записаны в
[машинном отчёте](../validation/tasks-runtime-a1.json). Все 17 production Python
файлов `app/` и `scripts/`, изменённых в A, совпадают с исходным commit A.
Изменения A.1 затрагивают только тесты, validation runner и документацию.

## SQLite 3.7.17 и read-only URI

Используемая конструкция проверена непосредственно:

```python
sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)
```

Подтверждено:

- `file:///...?...` и `mode=ro` распознаются SQLite 3.7.17;
- путь с пробелами, `#`, `?`, `%` и кириллицей правильно percent-encoded;
- SELECT работает; INSERT и CREATE TABLE через тот же connection запрещены;
- отсутствующий файл не создаётся, в каталоге не появляются побочные файлы;
- готовая база после read-only операций побайтово не меняется;
- startup и обычный рендер вообще не открывают новый storage.

Runtime source ID совпадает с историческим
[релизом SQLite 3.7.17](https://www.sqlite.org/releaselog/3_7_17.html).
Исторический [релиз 3.7.7](https://www.sqlite.org/releaselog/3_7_7.html) фиксирует
введение URI filenames; [исходник CPython v3.6.8](https://github.com/python/cpython/blob/v3.6.8/Modules/_sqlite/connection.c)
показывает передачу `SQLITE_OPEN_URI` при `uri=True`. Документы дополняют
выполненные тесты, а не заменяют их. Замена URI-механизма не требуется.

| SQL / SQLite feature | Что подтверждено |
| --- | --- |
| PRAGMA | table_info, index_list, index_info, database_list, busy_timeout, foreign_keys, journal_mode |
| DDL / IF NOT EXISTS / indexes | Создание служебной схемы и обычных индексов; повторное создание IF NOT EXISTS; unique constraints |
| Транзакции | BEGIN IMMEDIATE, commit; принудительная ошибка после DDL полностью откатывает схему, повтор проходит |
| Идемпотентность | Повторная миграция новой БД не меняет её байты |
| Foreign keys | Включение и фактическое отклонение нарушения в синтетической БД; новый Tasks bootstrap FK пока не создаёт |
| Query syntax | Реальные запросы badge/schema и legacy-регрессии выполняются; SQL compatibility gate проходит |
| busy handling | При EXCLUSIVE lock badge возвращает локальный 503 быстрее секунды; connect timeout 50 мс |
| progress handler | Длительный настоящий SELECT прерван примерно через 0,10 с |
| row factory | sqlite3.Row и преобразование результата в dict работают |
| ATTACH/DETACH | Новый Tasks не использует ни ATTACH, ни DETACH. Сохранённый legacy parameterized ATTACH проверен на fixtures |
| WAL | Новый storage использует DELETE journal; WAL не включается. Отдельный WAL fixture читается с writer и после его закрытия в доступном для записи временном каталоге |

Последняя проверка не доказывает возможность чтения WAL на read-only filesystem
без доступных `-wal`/`-shm`. Такая конфигурация новому модулю не требуется;
при ошибке badge/module endpoint возвращает локальный 503, без перехода на RW.
`immutable`, `query_only`, UPSERT, RETURNING, partial indexes, новые table-valued
PRAGMA, connection.autocommit и другие новые возможности для этого фундамента
не используются.

## Linux/POSIX locking и startup

Использован настоящий Linux `fcntl`, без Windows shim. Отдельный Python-процесс
удерживает одновременно `tasks.db` и `tasks-module.db` в `BEGIN IMMEDIATE`, затем
в `BEGIN EXCLUSIVE`. Пока он ещё жив и держит locks, два потока одновременно
выполняют настоящие GET `/app/orders` и `/app/products` через Flask test client.
Ответы — 200; счётчик попыток открыть Tasks storage — 0. В проверенных прогонах
пара запросов завершалась приблизительно за 0,12–0,17 с. Отдельный badge при
EXCLUSIVE lock реально получает 503: это положительный контроль наличия lock.

Startup matrix запускает настоящий `app/web.py`, а не заглушку ERP:

| Сценарий | Основная ERP | Новый Tasks |
| --- | --- | --- |
| Flag off, база отсутствует | Flask запущен, 5 основных страниц 200 | Route 404, storage не открывается, файл не создаётся |
| Flag off, неправильная schema | Flask запущен, страницы 200 | База не открывается и не изменяется |
| Flag on, база отсутствует | Flask запущен, страницы 200 | Собственный status возвращает 503 |
| Flag on, неправильная schema | Flask запущен, страницы 200 | Собственный status возвращает 503, repair отсутствует |
| Tasks import выбрасывает exception | Flask запущен, страницы 200 | Регистрация неуспешна и локально отключена |
| Частичная регистрация Blueprint | ERP доступна | Уже добавленные views закрыты error boundary |
| Импорт/функция миграции подготовлены выбросить exception | Flask и страницы работают | Импорт и функция миграции вообще не вызываются |
| Ошибка внутри явного offline bootstrap после DDL | ERP не импортируется | Только собственная транзакция откатывается; повтор возможен |

Проверены Orders, Products, Sales, Receipts и Inventory. Existing assignments
работают без legacy task tables, включая history, Inbox и responsibility API.
Это сохранение действующего collaboration, а не новый Inbox.

## Результаты

- 89 backend tests: PASS, 0 failures, 0 errors, 0 skipped.
- Реальный compiler Python 3.6.8: 22 файла, включая все 19 Python-файлов A.
- SQL compatibility gate: PASS, 20 файлов.
- Runtime DDL gate на Python 3.6.8/Linux: PASS, 20 migration modules,
  0 ensure functions, никаких новых runtime DDL.
- Explicit offline CLI: PASS под тем же интерпретатором, с запрещённым импортом ERP/catalog.
- `git diff --check`: PASS.

Локальные полные логи: `qa-reports/tasks-a1-runtime.log`,
`qa-reports/tasks-a1-runtime.json`, `qa-reports/tasks-a1-ddl.json`.
Санитизация пользовательских данных не требовалась: данных production в них нет.

## Что не доказано этим запуском

На локальном Windows компьютере точного Linux runtime нет. Это ограничение
обойдено запуском реального runtime на сервере в изолированной копии.
Непроверенными остаются реальные данные/нагрузка, live Gunicorn workers,
reverse proxy и production permissions будущей `tasks-module.db`: новый код
не развёрнут, рабочие БД не использовались. Linux syscall/file locking проверен,
но это не нагрузочный тест и не доказательство изоляции ресурсов ОС между всеми
модулями. Авторизация и Flask-процесс остаются общими, как согласовано в A.

## Безопасные проверки перед будущим deploy

1. Read-only сверить версии Python/SQLite/Flask, active release и состояние
   сервиса. Проверить, что candidate соответствует проверенному commit.
2. Экспортировать candidate в новую временную копию с LF, без `.env`/рабочих
   данных; не подменять действующий release и не устанавливать зависимости.
3. В этой копии повторить runner ниже от непривилегированного пользователя
   без сети. Он сам создаёт только synthetic fixtures. Не передавать пути
   к `/opt/clock-erp/instance` и не копировать credentials.
4. В staging проверить authenticated GET основных страниц и sidebar при
   выключенном модуле; затем startup failure matrix на тестовых БД. Для badge
   допустимы 200 или локальный 503, основные страницы должны оставаться 200.
5. Проверить deploy-конфигурацию: новый Tasks выключен, нет автоматического
   вызова его миграции при startup/request. Подготовку backup/restore нового
   business storage выполнять только при будущем согласованном включении.

Нельзя ради smoke теста портить/переименовывать рабочую базу, выполнять на ней
DDL/repair, удерживать lock или вызывать мутационные ERP endpoints.

Пример команды внутри уже подготовленной `/tmp` копии; значения путей подставляются
оператором для disposable-каталога. `env -i` и `unshare -n` применяются при запуске
через доступного непривилегированного пользователя, как в выполненной проверке:

```sh
python -B scripts/validate_tasks_runtime.py --report ../runtime-validation.json
python -B scripts/check_runtime_ddl.py --json
```

Runner отвергает другой runtime, root, live source path, `.env`, заданные внешние
интеграции/БД и отсутствующий обязательный файл/набор тестов. Если в среде нет
безопасной isolated copy и network namespace, не запускать проверку на рабочей ERP.

## Изменённые файлы A.1

| Файл | Изменение |
| --- | --- |
| `scripts/validate_tasks_runtime.py` | Воспроизводимый exact-runtime runner, compiler, fixture/network guards, JSON evidence |
| `tests/test_tasks_runtime_compat.py` | URI/RO, missing file, CLI, DDL rollback, PRAGMA/FK/indexes, progress handler, WAL probes |
| `tests/test_tasks_isolation.py` | Real startup matrix и отдельный Linux lock process с concurrent GET |
| `tests/test_tasks.py` | Совместимый с Python 3.6 trace callback |
| `docs/validation/tasks-runtime-a1.json` | Фактический runtime, результаты и hashes проверенных файлов |
| `docs/architecture/tasks-runtime-compatibility-a1.md` | Этот отчёт |
| `docs/architecture/tasks-isolation-stage-a.md` | Ссылка на закрытие первоначального ограничения |
| `docs/document-register.md` | Регистрация результата A.1 |

Дальнейшие функциональные этапы не начаты. После локального commit работа
останавливается для проверки владельцем.
