# Реестр документации Vechasu ERP

2026-10-05: `docs/multiwarehouse-local.md` — draft / implemented-local:
изолированные складские остатки и перемещения, минимальный UI, карта writers,
синтетические проверки и ограничения; добавлен loopback-only демо-сервер
`scripts/preview_multiwarehouse.py` с временной БД и запретом внешних подключений.
По актуальному решению владельца пилот сужен до TTT + Гонконг; новый WB lifecycle
и создание склада WB исключены, прежние продажи WB сохранены. Amazon → HK,
Bitrix только для сверки, сохранение истории при аннулировании поставки.
Локально завершены селекторы продаж/заказов/поставок/списаний, инвентаризация HK,
защита от использования TTT при нехватке HK и сохранение склада при возврате.
Обычный импорт новой карточки Bitrix с начальным остатком TTT сохранён;
сверка статуса сайта не менялась. Excel и регулярность брендов пока явно TTT.
Автоматические marketplace-события и live rollout не входят в этот этап;
владелец разрешил подготовку commit/Draft PR и CI, но не merge/deploy.
Связанные текущие контракты
`catalog-schema-migrations.md` и `receipt-supplies.md` дополнены локальной оговоркой.

2026-10-02: `docs/cdek-payouts.md` — самостоятельный модуль выплат СДЭК,
фильтры, финансовая сверка, импорт номеров, отдельные service/timer.
Реализовано локально; источник: `app/services/cdek_payouts.py`,
`app/cdek_payouts_routes.py`, `tests/test_cdek_payouts.py`. Публикация и
установка таймера не выполнены. Статус: implemented-local.

2026-10-02: локально добавлена автоматическая высота описания задачи от пяти
строк; контракт описан в `docs/design/README.md`. Публикация не выполнена.

2026-10-02: локальное уточнение блока микрозадач в `docs/design/README.md`:
автосворачивание пустого списка с сохранением ручного выбора, однострочная
подпись и центрированная стрелка. Новая правка ещё не опубликована.

2026-10-02: `docs/design/README.md` дополнен локальным контрактом карточек
проектов в задачах: один проект на строку полной ширины, ERP tokens, владелец и счётчики; без
изменения API. Browser smoke и публикация не подтверждены.
Добавлен явный возврат к проектам/задачам, доступный также при ошибке загрузки.
Уменьшена высота строк проектов и входящих, иконок и внутренних отступов.

2026-09-25: в `docs/design/README.md` описана компоновка карточки позиции
в рабочей ветке: широкое название, пары полей, модель в дополнительных
настройках и визуально отделённая группа внешних ссылок. Основание — шаблон
`warehouse.html` и `warehouse.css`; запись не подтверждает deploy.

Реестр составлен 2026-08-14 по чистому `origin/main` на commit
`975ef2572edfbf3568c5fac430d31f9d79af1d23`. Production не проверялся: задача
не разрешает SSH или deploy. Дата в колонке «Проверено» взята только из самого
документа либо обозначает текущую сверку с кодом; выдуманных дат нет.

Статусы: `current`, `draft`, `deprecated`, `archive`, `unknown` — определения
приведены в [главной навигации](README.md#статусы-документов).

| Текущий путь | Назначение | Подтверждение кодом | Статус | Проверено | Связанный модуль | Рекомендуемый раздел | Обновление | Противоречие | Действие |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `docs/cdek-delivery.md` | Статусы СДЭК в заказах и продажах, сверка проблем и работа менеджера | `app/clients/cdek.py`, `app/services/cdek_delivery.py`, `app/services/cdek_sales.py`, `tests/test_cdek*.py`; read-only API smoke и изолированный Flask smoke | `active` | 2026-10-01 | заказы, продажи, доставка | technical/integrations | да | Базовая интеграция PR #635, UI PR #636; UI и работа менеджера выпущены в PR #637; панель ручного/автоматического обновления проверяется локально | Сохранять production drop-in для systemd 219 |
| `AGENTS.md` | Правила работы Codex с проектом | Не код; применимые инструкции прочитаны полностью | `current` | 2026-08-14 | весь проект | корень | да | Да: верхний автоматический режим расходится с нижними правилами и `docs/agent-pipeline.md` | Оставить; владельцу унифицировать правила |
| `README.md` | Корневая точка входа | Содержит компактную карту основной документации | `current` | 2026-08-14 | весь проект | корень | нет | нет | Оставить минимальным |
| `.github/pull_request_template.md` | Шаблон описания и проверок Pull Request | `.github/workflows/tests.yml` подтверждает CI; разрешения на выпуск задаются не кодом | `current` | 2026-08-14 | quality, operations | `.github/pull_request_template.md` | да | Да: запрещает автоматические merge/deploy, а приоритетный раздел `AGENTS.md` требует полный автоматический выпуск | Оставить на месте; унифицировать после решения владельца |
| `.github/ISSUE_TEMPLATE/codex-task.yml`, `config.yml` | Шаблон продуктовой задачи и настройки создания Issues | Структура используется GitHub; поведение внешнего UI не проверялось | `current` | 2026-08-14 | product, quality | `.github/ISSUE_TEMPLATE/` | да | Да: прямо говорит, что задача не разрешает автоматический deploy, в отличие от приоритетного раздела `AGENTS.md` | Оставить на месте; унифицировать формулировки после решения владельца |
| `docs/README.md` | Карта разделов, статусы и правила документации | Сверено с деревом `origin/main` | `current` | 2026-08-14 | документация | `docs/README.md` | нет | нет | Оставить |
| `docs/document-register.md` | Инвентаризация и план будущего размещения | Сверено с Git-деревом и выбранными реализациями | `current` | 2026-08-14 | документация | `docs/document-register.md` | поддерживать | нет | Обновлять при каждом изменении документов |
| `docs/product/README.md` | Карта существующих продуктовых модулей, URL, действий и тестов | Сверено с Flask routes, services, templates и test tree | `current` | 2026-08-14 | product | без изменений | поддерживать | нет | Обновлять при изменении подтверждённого product contract |
| `docs/architecture/README.md` | Фактическая архитектура и границы current/legacy/planned/unknown | Сверено с `app/`, `frontend/`, routes и storage code | `current` | 2026-08-14 | architecture | без изменений | поддерживать | нет | Не добавлять production-факты без проверки |
| `docs/design/README.md` | Текущие tokens, primitives, shell, page chrome и UI states | Сверено с CSS, включая единый `erp-native-table-columns.css`, shared JS и Jinja templates | `current` | 2026-09-23 | design | темы `classic` и `dark` | поддерживать | нет | Поддерживать общий визуальный контракт без изменения page-specific поведения |
| `docs/ux/README.md` | Навигация, query state, поиск, фильтры, таблицы и responsive behavior | Сверено с routes, templates, shared JS, единым CSS столбцов и browser tests | `current` | 2026-09-23 | UX | без изменений | поддерживать | нет | Фиксировать общий visual contract отдельно от различий поведения страниц |
| `docs/quality/definition-of-done.md` | Практический checklist готовности изменения | Команды и примеры сверены с CI, package scripts и test tree | `current` | 2026-08-14 | quality | без изменений | поддерживать | нет | Применять только релевантные проверки |
| `docs/agent-pipeline.md` | Процесс ветка → проверки → Draft PR → merge → deploy | `.github/workflows/tests.yml` и `scripts/deploy.sh` существуют; управленческие разрешения кодом не подтверждаются | `unknown` | 2026-08-14 | разработка, operations | `docs/operations/agent-pipeline.md` | да | Да: запрещает автоматические merge/deploy, а приоритетный раздел `AGENTS.md` требует их автоматически | Не перемещать до решения владельца |
| `docs/article-duplicates-audit-2026-08-11.md` | Снимок дублей артикулов production | Защита новых дублей есть в `app/services/excel_product_catalog.py`; сами production-строки не перепроверялись | `archive` | 2026-08-11 | каталог товаров | `docs/archive/audits/article-duplicates-2026-08-11.md` | нет | нет | Переместить после подтверждения политики архивов |
| `docs/bitrix_catalog_dry_run.md` | Результат read-only dry-run Bitrix | `scripts/bitrix_catalog_dry_run.py` существует; внешние числа не перепроверялись | `archive` | 2026-07-20 | Bitrix catalog | `docs/archive/audits/bitrix-catalog-dry-run-2026-07-20.md` | нет | нет | Архивировать на следующем этапе |
| `docs/bitrix_catalog_endpoint_verification.md` | Проверка внешнего export endpoint | `bitrix/catalog-export.php` существует; внешний endpoint и server state не проверялись | `archive` | 2026-07-20 | Bitrix catalog | `docs/archive/audits/bitrix-catalog-endpoint-2026-07-20.md` | да | нет | Сохранить как историческое доказательство |
| `docs/bitrix_catalog_import_report.md` | Отчёт о production-импорте каталога | Импортёр и схема есть; production-результаты не перепроверялись | `archive` | 2026-07-20 | Bitrix catalog, database | `docs/archive/operations/bitrix-catalog-import-2026-07-20.md` | нет | нет | Архивировать после подтверждения |
| `docs/bitrix_catalog_research.md` | Сводка исследования каталога и предложений | Клиент, импортёр и sync-код существуют; документ смешивает снимок и планы | `unknown` | 2026-07-20 | Bitrix catalog | `docs/technical/integrations/bitrix-catalog.md` | да | Частично: «текущий код» относится к старому baseline | Разделить актуальный контракт и архив исследования |
| `docs/bitrix_catalog_server_research.md` | Снимок структуры сервера Bitrix | Диагностический PHP-скрипт существует; сервер не проверялся | `archive` | 2026-07-20 | Bitrix catalog | `docs/archive/audits/bitrix-server-2026-07-20.md` | да | нет | Архивировать; удалить абсолютные server paths из будущего current-документа |
| `docs/bitrix_catalog_sync.md` | Runbook синхронизации каталога и статуса сайта | Полный импорт остаётся ручным; status-only сервис, компактный UI, server-side списки расхождений и timer 03:00 существуют | `current` | 2026-09-26 | Bitrix catalog | `docs/operations/bitrix-catalog-sync.md` | да | нет | Контролировать возраст последней успешной сверки |
| `docs/bitrix_excel_product_reconciliation.md` | Dry-run сопоставления Bitrix и Excel | Сервис/скрипт сопоставления есть; локальный Excel и результаты не проверялись | `archive` | 2026-07-22 | каталог, Excel | `docs/archive/audits/bitrix-excel-reconciliation-2026-07-22.md` | да | Да: содержит абсолютный локальный путь к исходному Excel | Архивировать; privacy/path review |
| `docs/bitrix_orders_import_research.md` | Исследование чтения заказов Bitrix и проект хранения | Клиенты заказов и маршруты есть; предлагаемое хранилище не подтверждено | `unknown` | 2026-07-20 | Bitrix orders | `docs/technical/integrations/bitrix-orders.md` | да | Частично: текущие детали требуют повторной сверки | Разделить факты, историю и предложения |
| `docs/catalog_data_quality_report.md` | Production-аудит качества каталога | Audit-скрипт/сервис существуют; метрики production не перепроверялись | `archive` | 2026-07-20 | каталог, database | `docs/archive/audits/catalog-data-quality-2026-07-20.md` | нет | нет | Архивировать после подтверждения |
| `docs/critical_products_excel_receipt_recovery.md` | Отчёт о восстановлении данных и изменении Excel-прихода | Сервисы recovery/import существуют; production-состояние историческое | `archive` | неизвестно | товары, приходы, operations | `docs/archive/operations/products-excel-recovery.md` | да | нет | Сохранить как incident report; добавить дату при подтверждении |
| `docs/full-react-rewrite-api-map.md` | Целевой REST API и частичный срез Stage 2 | Многие `/api/v1` routes есть, но заявленный полный контракт не реализован | `draft` | 2026-07-30 | API, frontend | `docs/technical/api/full-react-target.md` | да | Да: описывает удалённые/отсутствующие React feature-модули как Stage 2 | Не использовать как current API; переснять фактический manifest |
| `docs/full-react-rewrite-audit.md` | Технический аудит старого baseline и целевая архитектура | Часть архитектуры узнаваема, но номера строк, размеры, route count и frontend inventory устарели | `archive` | 2026-07-29 | весь проект | `docs/archive/audits/full-react-rewrite-2026-07-29.md` | да | Да: утверждает отсутствие `frontend/package.json`/React-инфраструктуры и старые counts; текущий Git содержит их | Архивировать без переписывания истории |
| `docs/full-react-rewrite-feature-matrix.md` | Матрица parity для программы React | Baseline `2212988`; текущие routes и UI существенно изменены | `archive` | 2026-07-29 | product, frontend | `docs/archive/product/full-react-feature-matrix-2026-07-29.md` | да | Да: статусы переноса относятся к прошлому этапу | Создать новую матрицу только при возобновлении программы |
| `docs/full-react-rewrite-risk-register.md` | Риски планируемой React/PostgreSQL программы | Риски частично применимы, но программа и owners не подтверждены | `draft` | 2026-07-29 | architecture, security | `docs/architecture/full-react-risk-register.md` | да | нет | Сохранить как proposal до решения владельца |
| `docs/full-react-rewrite-roadmap.md` | План 21 этапа React/PostgreSQL-перехода | План не является реализацией; текущий статус программы неизвестен | `draft` | 2026-07-29 | product, architecture | `docs/product/full-react-roadmap.md` | да | Частично: часть этапов исторически выполнялась/откатывалась | Перебазировать план только по решению владельца |
| `docs/full-react-rewrite-stage-1-baseline.md` | Отчёт первого этапа React-программы | Vite-инфраструктура есть, но feature source tree из отчёта отсутствует | `archive` | 2026-07-29 | frontend, quality | `docs/archive/reports/full-react-stage-1-2026-07-29.md` | нет | Да: baseline не отражает текущий frontend | Архивировать |
| `docs/full-react-rewrite-stage-2-report.md` | Отчёт Stage 2 по товарам, приходам и продажам | Текущий `frontend/src` не содержит описанных feature modules | `archive` | 2026-07-30 | frontend, API | `docs/archive/reports/full-react-stage-2-2026-07-30.md` | да | Да: реализованный тогда React-срез отсутствует в source tree `origin/main` | Архивировать; владельцу подтвердить историю отката |
| `docs/full-react-rewrite-ui-map.md` | Карта старого UI и проект React-компонентов | Jinja/CSS существуют, но baseline и будущий component list не являются текущим стандартом | `archive` | 2026-07-29 | design, UX, frontend | `docs/archive/design/full-react-ui-map-2026-07-29.md` | да | Частично: смешаны исторические факты и будущие правила | Разделить при создании утверждённых design/UX docs |
| `docs/mvp-performance-audit.md` | Замеры и оптимизации четырёх MVP-разделов | Benchmark-скрипт и server pagination существуют; числа не повторялись | `archive` | 2026-07-31 | performance | `docs/archive/audits/mvp-performance-2026-07-31.md` | да | нет | Архивировать; методику вынести отдельно при новых замерах |
| `docs/screenshots/**` | Визуальные снимки UI, включая compact modals, Stage 2 и отдельные страницы | Файлы присутствуют; соответствие текущему UI и отсутствие чувствительных данных визуально не проверялись | `unknown` | неизвестно | design, UX, quality | `docs/archive/screenshots/` либо утверждённый visual baseline | да | Stage 2 снимки относятся к историческому React-этапу | Провести privacy и актуальность review до перемещения |
| `docs/owner_feedback_audit.md` | Отчёт о реализации пожеланий владельца | Часть маршрутов/хранилищ существует; ветка и поведение исторические | `archive` | 2026-07-22 | product, UX | `docs/archive/audits/owner-feedback-2026-07-22.md` | да | Частично: состояние «до/после» не равно текущему стандарту | Архивировать |
| `docs/orders-progressive-disclosure.md` | Текущий интерфейс, статусы, локальное удаление и диагностика списка заказов | Сверено с `_orders_list_results.html`, `orders.css`, локальной tombstone-механикой и целевыми тестами | `current` | 2026-09-23 | orders, UX, frontend, backend | без изменений | поддерживать | нет | Обновлять при изменении контракта списка заказов |
| `docs/products_ui_regression_audit.md` | Разбор регрессии `/products` после PR #9 | Текущий `/products` снова перенаправляет на `/warehouse` | `archive` | неизвестно | товары, UX | `docs/archive/audits/products-ui-regression.md` | нет | Нет для исторического отчёта; его промежуточное состояние устарело | Архивировать |
| `docs/receipt-supplies.md` | Локальные поставки и идемпотентные дополнения | SupplyEngine, общий ERP picker, receipt ledger, BEGIN IMMEDIATE | `current` | 2026-09-08 | receipts, stock, frontend | без изменений | поддерживать | нет | Обновлять при изменении контракта поставок |
| `docs/receipt-catalog-audit-pr116.md` | Разбор PR #116 и routing React/Jinja | Текущие `/products`, `/sales`, `/receipts` обслуживаются Jinja, а `/app/*` перенаправляет на `/app/products` | `archive` | 2026-07-30 | receipts, frontend | `docs/archive/audits/receipt-catalog-pr116.md` | да | Да: раздел «исправление подключает React» не соответствует текущему routing | Архивировать; не использовать как current architecture |
| `docs/unified-catalog-migration.md` | Runbook миграции `unified_catalog_v1` | Скрипт, таблицы links/ambiguities и сервис чтения существуют | `current` | 2026-08-14 | database, catalog | `docs/operations/unified-catalog-migration.md` | да | нет | Переместить на следующем этапе; добавить preflight/owner approval |
| `docs/production-sqlite-migrations.md` | Exact-runtime SQLite preflight, migration ledger и rollback | `app/schema_migrations.py`, `scripts/migration_preflight.py` и `scripts/deploy.sh` | `current` | 2026-08-24 | database, operations | без изменений | поддерживать | нет | Обновлять с каждым новым migration contract |
| `docs/runtime-ddl-audit-2026-08-25.md` | Runtime DDL inventory, startup trace, schema drift и план удаления legacy ensure | Static AST inventory и exact production-runtime rehearsal | `current` | 2026-08-25 | database, backend, operations | без изменений | поддерживать до PR-G | нет | Обновлять каждым migration slice; архивировать после empty runtime allowlist |
| `docs/domain-schema-migrations.md` | Versioned auth/orders/customers/comments migrations | Domain runner, runtime validators, deploy integration и regression tests | `current` | 2026-08-25 | database, backend, operations | без изменений | поддерживать | нет | Обновлять при изменении domain schema contract |
| `docs/catalog-schema-migrations.md` | Versioned complete catalog schema lifecycle | Catalog runner, full manifest, sentinel-independent runtime validator and rollback | `current` | 2026-08-25 | database, backend, operations | без изменений | поддерживать | нет | Обновлять при изменении catalog schema contract |
| `docs/unified-feedback-audit.md` | Контракт глобальных уведомлений | `_sidebar.html`, `notifications.js` и `tests/test_global_notifications.py` подтверждают ядро | `current` | 2026-08-14 | frontend, UX | `docs/technical/frontend/global-feedback.md` | да | нет | Переместить и отделить UX-правила от реализации |
| `docs/decisions/README.md` | Список неподтверждённых ADR-кандидатов | Предложения не объявлены реализацией | `current` | 2026-08-14 | architecture | `docs/decisions/README.md` | нет | нет | Ждать решений владельца |
| `docs/templates/product-module.md` | Шаблон продуктового модуля | Не применимо | `current` | 2026-08-14 | документация | без изменений | нет | нет | Использовать для новых product docs |
| `docs/templates/technical-document.md` | Шаблон технического документа | Не применимо | `current` | 2026-08-14 | документация | без изменений | нет | нет | Использовать для technical docs |
| `docs/templates/adr.md` | Шаблон ADR с четырьмя допустимыми статусами | Не применимо | `current` | 2026-08-14 | документация | без изменений | нет | нет | Создавать ADR только после подтверждения решения |
| `docs/templates/operations-runbook.md` | Шаблон операционного runbook | Не применимо | `current` | 2026-08-14 | документация | без изменений | нет | нет | Использовать для operations docs |

## Отдельный список противоречий

1. `AGENTS.md` одновременно требует автоматический полный выпуск в приоритетном
   разделе и запрещает автоматические merge/deploy в нижних разделах;
   `docs/agent-pipeline.md` поддерживает второй вариант.
2. Набор `full-react-rewrite-*` фиксирует baseline 2026-07-29/30 и React feature
   tree, которого нет в текущем `origin/main`; текущие production-facing routes
   снова используют Jinja, а `/app/*` не обслуживает описанные страницы.
3. `full-react-rewrite-audit.md` содержит точные counts, line references и
   утверждение об отсутствии frontend package infrastructure, которые не
   соответствуют текущему дереву.
4. `receipt-catalog-audit-pr116.md` утверждает, что исправление подключает React
   к `/products`, `/sales`, `/receipts`; текущий код обслуживает эти адреса
   Jinja-маршрутами.
5. `bitrix_excel_product_reconciliation.md` содержит абсолютный путь к локальному
   пользовательскому файлу; это историческое происхождение данных, а не
   переносимый runbook.

Отдельный пробел реализации, выявленный при сверке: current routes `/overview`,
`/orders` и `/analytics` ссылаются на отсутствующие templates. Код в рамках
документационной задачи не изменяется.

Незакоммиченная дизайн-система, `PageHeader`, CSS и `.save` из старого worktree
не исследовались, не копировались и не использовались как источник истины.

## Проведение WB — 2026-09-07

`docs/wildberries-sale-posting.md` — `current`, контракт реализации проведения
WB через общий сервис продаж; код `app/services/wildberries_sales.py`,
маршрут и шаблоны проверяются `tests/test_wildberries_sales.py`. Статус
документа описывает код ветки и не означает выполненный production deploy.

## Восстановление WB — 2026-09-07

`docs/wildberries-recovery.md` — `current` для кода восстановления через
существующие snapshot/matching/journal. Реальные recovery-записи требуют
отдельного подтверждения dry-run; документ не подтверждает production deploy.

2026-09-07: в `docs/product/README.md` зафиксирован отказ от ручного создания товаров и сохранение добавления из Bitrix; подтверждение — целевые API/UI-тесты.

2026-09-08: в `docs/product/README.md` зафиксирован контракт добавления количества из Bitrix: сохранение существующей карточки, пользовательское количество через штатный приход, внешний остаток только справочный; проверки — `test_single_bitrix_product_import.py`, `test_supplies.py`.

- 2026-09-15: в `product/README.md` уточнён текущий контракт проведения заказа:
  расчёты Bitrix не блокируют продажу; проверено по `app/web.py` и целевым
  тестам `test_order_tictactoy_sale.py`, `test_orders_rework.py`.

- 2026-09-25: [Обязательный ремешок](required-straps.md) — current: признак товара, ручной выбор ремешка только бренда часов, атомарное списание, отображение состава в продажах и исторический возврат; tests/test_required_straps.py. Статус не подтверждает production deploy.
`docs/sales-navigation.md` — `current`, 2026-09-25: переход после проведения Tictactoy, подсветка без фильтра и явный просмотр одной продажи; production deploy не подтверждён.

2026-09-25: в `docs/orders-progressive-disclosure.md` описаны dropdown статусов и компактный индикатор синхронизации; backend-контракт не менялся.

2026-09-26: в `docs/orders-progressive-disclosure.md` уточнено разделение здоровья синхронизации WB и счётчика замечаний сверки; общий индикатор и диагностика используют один расчёт, backend и расписание не менялись.

2026-09-30: в `docs/bitrix_catalog_sync.md` уточнён общий отбор для счётчиков сверки и таблицы товаров: видимость импорта и исключение позиций действующей инвентаризации.
`docs/architecture/tasks-boundary.md` — `current`, 2026-09-26: удаление старых ERP-привязок задач с сохранением данных, общих назначений, почты и уведомлений; схема базы не меняется, production deploy не подтверждён.

2026-09-26: `docs/architecture/tasks-isolation-stage-a.md` — `current` для ветки
`codex/tasks-isolation-stage-a`: независимый от Tasks рендер ERP, асинхронные
счётчики, отдельная проверка collaboration и выключенный фундамент нового
модуля с `tasks-module.db`. Связанный `tasks-boundary.md` уточнён; схема legacy
`tasks.db` сохранена. Документы не подтверждают merge или production deploy.

2026-09-26: `docs/architecture/tasks-runtime-compatibility-a1.md` — `current`
для `codex/tasks-runtime-a1`: проверка фундамента A на Linux / Python 3.6.8 /
SQLite 3.7.17, только временная копия и синтетические данные. Production-код
этапа A не изменён, добавлены воспроизводимые проверки и исправлен legacy
test trace callback. Merge, push и deploy не выполнялись.

2026-09-26: `docs/architecture/tasks-core-stage-b.md` — `current` для
`codex/tasks-core-stage-b` от принятого `6d06b8f`: новый normal Task core,
единые permissions, локальные activity/transactions, version, soft delete,
API и schema v2 новой tasks-module.db. По умолчанию OFF, UI и legacy не
переключены. `tasks-boundary.md` уточнён: legacy data migration не предусмотрена.
`docs/validation/tasks-core-b-runtime.json` и `tasks-core-b-ddl.json` фиксируют
134 успешных теста на Linux / Python 3.6.8 / SQLite 3.7.17 и runtime DDL gate.
Только временные fixtures; merge, push, deploy не выполнялись.

2026-09-26: `docs/architecture/tasks-core-review-fixes-b1.md` — `current` для
`codex/tasks-core-stage-b1` от `c5c1762`: строгий schema contract, сохранение
HTTPException status, bounded JSON body на Werkzeug 2.0.3 и UTF-8 validation.
24 новых regression tests; 158 обязательных тестов прошли на Linux / Python 3.6.8 /
SQLite 3.7.17. Результаты: `docs/validation/tasks-core-b1-runtime.json`,
`docs/validation/tasks-core-b1-ddl.json`. Schema v2 и бизнес-функции не менялись.
`docs/technical-debt/tasks-query-performance.md` — `deferred`, TD-TASKS-001:
оптимизация SQL/индексов отложена до измерений перед performance acceptance.
Auth sessions не переделаны, legacy не переносится; push, merge, deploy запрещены.

2026-09-26: `docs/architecture/tasks-projects-stage-c.md` — `current` для
`codex/tasks-projects-stage-c` от принятого B.2 `beb68e8`: schema v3 Projects,
members, visibility, scopes/views, archive, counters и персональный dashboard.
Уточняет контракты Stage B для этой ветки; flag OFF, без UI и legacy migration.
`docs/validation/tasks-projects-c-runtime.json` и `tasks-projects-c-ddl.json`:
208 успешных tests на Linux / Python 3.6.8 / SQLite 3.7.17 / Flask 2.0.3 /
Werkzeug 2.0.3, DDL gate, SHA31 sources и query plans. Только synthetic fixtures,
без сети, от UID99. TD-TASKS-001 остаётся отложенным; production не переключён.

2026-09-27: `docs/architecture/tasks-roadmap-d-f.md` — `planned`: автоматический
цикл D/E/F с независимым review, согласованный через ветку «Задачи по ERP» по
прямому поручению владельца. Не является evidence завершения будущих этапов.
`docs/architecture/tasks-microtasks-stage-d.md` — `current` Stage D, final review PASS: schema v4,
точные UTC microtasks24h, recipient Inbox и отдельный notification claim.
`docs/validation/tasks-microtasks-d-runtime.json` и `tasks-microtasks-d-ddl.json`:
248/248 PASS на Linux/Python3.6.8/SQLite3.7.17/Flask2.0.3/Werkzeug2.0.3,
35 source hashes, DDL gate, synthetic fixtures UID99 без сети. Flag OFF.

2026-09-27: `docs/architecture/tasks-ui-stage-e.md` — `current`, final review PASS.
Отдельный UI entry `/app/tasks-module`, main/micro/Inbox/Projects/board/archive;
legacy не переключён. Три MEDIUM JS races/read dependency исправлены с 4 regression
checks; ещё 15 adapter checks PASS. `docs/validation/tasks-ui-e-runtime.json` и
`tasks-ui-e-ddl.json`: 257/257 PASS на exact production runtime, 38 source hashes,
DDL gate PASS. Feature OFF, без push/merge/deploy. LOW polish относится к Stage F.

2026-09-27: `docs/architecture/tasks-final-stage-f.md` — `current`, final/delta review
PASS, итог A–F PASS. 261/261 exact-runtime PASS, 22 JS checks PASS, browser
workflows/fault injection/themes/responsive, 32000-task synthetic benchmark.
`docs/validation/tasks-final-f-runtime.json`, `tasks-final-f-ddl.json`,
`tasks-final-f-performance.json` — воспроизводимые результаты; TD-TASKS-001 измерен.
Нет schema/business changes, legacy migration, push/merge/deploy/enable.

2026-09-27: `docs/operations/tasks-release-readiness.md` — `current`: локальная
подготовка exact-tree deploy classification и optional Tasks backup/recovery,
без изменения функциональности A–F. `docs/validation/tasks-release-runtime.json`:
368/368 exact-runtime PASS (Python 3.6.8, SQLite 3.7.17, Git 1.8.3.1), 0 skips,
0 обращений за пределы fixtures. Финальный manifest/diff review — перед commit;
production rehearsal/R3/merge/deploy/ON этим документом не разрешаются.

2026-09-27: `docs/architecture/tasks-boundary.md` — актуализирован для
`codex/tasks-legacy-retirement`: один новый раздел «Задачи», удаление legacy
UI/API/business implementation, 410 tombstones и safe redirect, сохранение
shared collaboration и всех старых данных. Прежние описания параллельного
старого/нового UI в отчётах A–F и R1/R2 являются историческим состоянием.
Одноразовый release manifest предыдущего выпуска удалён, schema gates сохранены.

`docs/validation/tasks-retirement-runtime.json` — exact Linux evidence retirement:
349/349 PASS, 0 skips, 50 source hashes, отсутствие внешних database paths.

2026-09-27: `docs/architecture/tasks-inbox-acceptance.md` — `current` для отдельной
preview-ветки: ознакомление без read, атомарное принятие normal, micro до выполнения,
раздельные badge и явная offline подготовка прежнего смысла waiting. Schema v4
не меняется. Это локальный owner trial, не утверждение production deployment.
Уточнение владельца: micro во входящих/preview только для ознакомления;
завершение в рабочем разделе. Последняя UX-полировка: красное число normal и
компактное ⚡N в одной строке sidebar/сводки; таймер — неинтерактивная светло-янтарная
плашка в геометрии кнопки принятия. Подсчёт и бизнес-логика не меняются.
Вкладка «Входящие» использует компактную группу без жёлтой плашки; заголовок
«⚡ Микрозадачи · 24 ч» и нейтральная сводка размещены в две строки без дублирования числа.
UI-уточнение входящих: единая геометрия строк, компактный таймер во втором
action-слоте и backend-приоритет micro до пагинации; другие вкладки без изменений.

2026-09-30: `docs/bitrix_catalog_sync.md` — current: отсутствующий товар или неизвестный ACTIVE после полной сверки сбрасывает сохранённый статус; прерванная сверка и обновление таблицы описаны по текущей реализации. Production deploy не подтверждён.

2026-10-05: `docs/cdek-delivery.md` — уточнены переходы по счётчикам, выделение
категорий и ручное обновление внутри сверки; проверка на изолированных данных,
production deploy не подтверждён.
