# Tasks: локальная подготовка выпуска после A–F

Статус: реализовано и проверено локально, ветка `codex/tasks-release-readiness`, база
`f19262ffffccf61a648689d7d97f838c09a546d3`. Не подтверждает deploy.
Принятая функциональность A–F и feature flag OFF не меняются.

## R1: классификация точного релиза

Первый release относительно `a0e53422c023bfaa8bdc40759b2f4587ea650d1c`
добавляет read-only auth adapters и расширяет SQL compatibility scanner.
Изменения `app/auth.py` / `app/schema_migrations.py` не меняют схемы auth/catalog,
но старый deploy классифицировал их по имени файла как ERP migrations.

`scripts/tasks_release_preflight.py` выполняет только локальные read-only Git
команды. Deploy получает helper из candidate commit и запускает до schema gate,
application update и изменения БД. `changed_files` остаётся полным; только вход
двух schema detectors заменён на `schema_changed_files`. Остальные migration
detectors и обязательный contract gate сохраняются. Tasks migration не добавлена
в deploy/startup/HTTP.

Одноразовый `ops/tasks-release-classification.json` содержит точный base commit,
version 1, два фиксированных non-schema paths и SHA256 отсортированных
NUL-separated записей `git ls-tree -r -z --full-tree` candidate: blob IDs, пути,
file modes. Исключён только сам manifest, чтобы избежать циклического hash.
Все остальные файлы, включая helper, deploy, contract, tests и docs, входят
в digest. Иной base, изменённый manifest или любое изменение дерева останавливает
preflight. Без manifest работают обычные detectors без исключений.

Manifest формируется по окончательному Git tree после всех исправлений/evidence
и проверяется вместе с diff. Squash с тем же деревом допустим; меняется commit
SHA, но не содержимое. Иной base требует нового решения. Постоянного skip flag нет.
Поддерживается Git 1.8.3.1: subprocess cwd вместо `git -C`; tests используют
`git init` + `symbolic-ref`, без `git init -b`.

## R2: optional DB и восстановление

Recovery contract содержит отдельную `optional_databases` только для новой
`tasks-module.db`. Прежние обязательные ERP DB остаются обязательными.

- Отсутствующая Tasks DB допустима и не создаётся для удобства проверки.
- Присутствующая DB проходит полный Tasks schema/ledger contract,
  `integrity_check=ok` и пустой `foreign_key_check`.
- Symlink, включая broken symlink, каталог вместо DB и повреждения вызывают отказ.
- Recovery вообще не импортирует Tasks validator. Выбранный release contract
  содержит fingerprint всех schema objects (SQL с игнорированием только пробелов
  и регистра ключевых слов), user_version и полный version/signature ledger.
  Строковые литералы сохраняются точно. Текущая реализация Tasks не определяет
  совместимость другой версии: синтетический v5 → v4 full restore проверяется
  по target contract. Format 1 не допускает эквивалентные переписывания DDL.
- Backup использует существующий SQLite backup механизм, включает DB ровно один
  раз и сохраняет schema в Recovery metadata.
- Data restore может вернуть утраченную optional DB из проверенного backup,
  но не может удалить существующую DB отсутствующим backup.
- Full restore также отказывает при потере существующей optional DB. Полные
  file/schema manifests и проверки обязательных ERP DB сохраняются.
- Отображаемые capabilities backup и engine используют одни проверки совместимости.

Rehearsal использует настоящие v4 fixtures: normal task с version 2, microtask
с точным UTC deadline, Project/member, Activity и Inbox. После SQLite backup,
tar и safe extraction сравниваются все строки семи таблиц, включая ID/версии/
timestamps. Копия проходит TasksRepository.status и write transaction с rollback.
Отдельный backup выполняется при открытой read transaction источника. Общие
Recovery tests проверяют полный engine на временных instance без system actions.

## Откат и условия перед ON

Первый шаг при сбое Tasks — flag OFF с контролируемым restart, сохранив новую DB.
Это описание процедуры, не разрешение её исполнять.
Recovery contract изменён содержательно: автоматический code rollback к
pre-Tasks contract остаётся **заблокирован** прежней проверкой contract hash.
Она не ослаблялась; нельзя обещать автоматический «код назад». Full restore
не обходит защиту от потери новой DB старым backup. Legacy не переносится,
новая DB не удаляется и не downgrade-ится.

Старый pre-deploy backup имеет другой contract hash: **data_restore под новым
кодом запрещён**, даже при отсутствующей Tasks DB. Пока Tasks DB отсутствует,
такой backup допускает full_restore соответствующего старого release при
сохранении остальных Recovery gates. После установки нового кода с OFF и до
дальнейших изменений обязательно создать и проверить новый backup с новым
contract. После создания Tasks DB повторить rehearsal. Старый contract сам по
себе игнорирует дополнительную Tasks DB и не изменяет её; это не разрешение
обойти защиту full_restore от потери уже существующей DB.

Путь вне `instance/` не покрывается общим backup. До ON обязательны:
realpath equality эффективного Tasks DB path и migration CLI target; runtime SHA;
service user/group; права файла/каталога для SQLite journal; active users ≤1000;
свободное место; backup configuration. Read-only server check показал `User=root`,
но это нужно перепроверить перед выпуском; тесты не заменяют фактическую проверку
прав service account. ON без созданной/проверенной DB недопустим.

После отдельно разрешённой migration при OFF: backup → restore в изолированное
место → integrity/FK/schema/ledger/status и сверка данных. Затем отдельное
решение о ON. Флаг глобальный, pilot allowlist не реализован.
UI при ON включает POST notifications/claim, поэтому его smoke не строго read-only
при наличии реальных Inbox events. Production CRUD smoke требует разрешения.

## Проверки и review

Локально Windows: 55 tests, 52 PASS и 3 POSIX skips; Bash syntax и Tasks isolation
включены. Тесты: test_tasks_release, test_deploy_availability,
test_tasks_isolation, test_tasks_module. Python 3.12 не заменяет exact runtime.

Exact runner: `scripts/validate_tasks_runtime.py --release --report <report>`:
требует Linux/Python 3.6.8/SQLite 3.7.17, nobody, исходники в `/tmp`, временные DB
и отсутствие production configuration. Добавляет backup/recovery/deploy suites,
записывает фактический git --version и проверяет Git команды на этом бинарнике.

Exact validation: **368/368 PASS**, 0 skips, Linux CentOS 7.9,
Python 3.6.8 / SQLite 3.7.17 / Flask 2.0.3 / Werkzeug 2.0.3 / Git 1.8.3.1.
Запуск от nobody (uid 99), `unshare -n`, source copy в согласованном `/tmp`;
7277 SQLite connections только в synthetic fixtures, denied paths = 0.
[Машинный отчёт](../validation/tasks-release-runtime.json) содержит source hashes,
runtime и isolation evidence. Код Tasks A–F не изменён.

Проверены точная классификация и её отказ на изменённом tree/base; schema defects
(NOT NULL/type/default/CHECK/PK/FK/index/ledger); optional absent/present/corrupt;
реальный backup/restore всех записей; active reader; journal/ownership; запрет
silent drop; target v4 из synthetic v5; old-contract data_restore STOP и допустимый
full_restore при отсутствии новой DB; capabilities совпадают с engine.

В первом exact прогоне найдены несовместимости существующих тестов: mock
call_args.args/kwargs (заменены tuple API), `git init -b` и семантика `git add .`
на Git 1.8. Тестовые пути backup HTTP/retention явно изолированы от серверного
source/release. Guard заблокировал попытки первоначальной обвязки открыть
production DB до соединения; финальный прогон таких попыток не делает.
Test fixtures используют настоящие backup/Git/SQLite, подменяются лишь запуск
системного recovery и фиксированный host path. Product auth не менялась.

Первый review R1/R2 выявил зависимость от текущего Tasks validator и неявную
смену backup contract; обе причины устранены и покрыты тестами. Финальный
manifest создаётся после всех code/docs/evidence изменений; до commit его digest
и полный diff передаются на отдельный review в указанной ниже ветке.

Это закрывает локальную реализацию R1/R2. Production rehearsal после разрешённой
migration, свежие факты R3, merge/deploy/ON остаются отдельными release gates.

Review: [Задачи по ERP](https://chatgpt.com/g/g-p-6aa80e13ea2881919639c9b041fc3174-erp-bitriks/c/6ab7c9f4-ef24-83ed-b0ca-ff67bd5f9b0d).
Push, merge, deploy, enable и production migration не выполняются.
