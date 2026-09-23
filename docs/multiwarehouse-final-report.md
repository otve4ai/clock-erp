# Финальный отчёт: мультисклад

Статус: production-ready candidate. Production не мигрировался, PR не merge, deploy не выполнялся.

## 1. Final HEAD

Проверенный implementation/merge HEAD: `e7d937be`. Финальный SHA ветки (следующий report-only commit) фиксируется в сообщении сдачи. Реализация выполнена в `/Users/maksim/Projects/clock-erp-multiwarehouse`, ветка `agent/multiwarehouse`; исходное дерево `/Users/maksim/Projects/clock-erp` не изменялось.

## 2. Commits

Commits задачи: `503edc6b` (canonical multiwarehouse backend), `46284c9a` (синхронизация с main), `74f8f23d` (read model и UI), `455d3f74` (production readiness, tests, rehearsal/evidence/runbook), `e7d937be` (merge актуального `origin/main` и интеграция configurable export). Следующий commit изменяет только реквизиты этого отчёта.

## 3. Pull Request

Draft PR: https://github.com/otve4ai/clock-erp/pull/596. Автоматический merge запрещён и не выполнялся.

## 4. Изменённые файлы

- Runtime: `app/schema_migrations.py`, `app/web.py`.
- Services: `business_analytics.py`, `category_consolidation.py`, `category_integrity.py`, `component_inventory.py`, `excel_product_catalog.py`, `inventory_control.py`, `out_of_stock.py`, `product_bundles.py`, `product_excel_export.py`, `receipt_inventory.py`, `receipt_recovery.py`, `sales_inventory.py`, `shared_catalog.py`, `supplies.py`, `warehouse_stock.py`, `wildberries_matching.py`.
- UI: `app/static/js/warehouse-multiwarehouse.js`, `app/templates/product_bundle.html`, `app/templates/warehouse.html`.
- Migration/test infrastructure: `scripts/run_backend_tests.py`, `scripts/verify_multiwarehouse_rehearsal.py`, `docs/runtime-ddl-inventory.json`, `frontend/scripts/multiwarehouse-rehearsal.mjs`.
- Tests: `tests/test_bitrix_erp_product_sync.py`, `test_brand_inventory.py`, `test_brand_management.py`, `test_business_analytics.py`, `test_catalog_filtering.py`, `test_catalog_stock_statistics.py`, `test_category_management.py`, `test_component_inventory.py`, `test_excel_product_catalog.py`, `test_multiwarehouse_stage5.py`, `test_order_strap_replacement.py`, `test_product_bundles.py`, `test_product_deletion.py`, `test_product_excel_export.py`, `test_runtime_ddl_gate.py`, `test_unified_catalog_inventory.py`, `test_wildberries_recovery.py`, `test_writeoffs.py`.
- Документация/evidence: `docs/multiwarehouse-final-audit.md`, этот отчёт, `docs/multiwarehouse-production-runbook.md`, `QA_EVIDENCE/multiwarehouse-rehearsal/`.

## 5. Архитектура

Канонический физический остаток хранится только в `erp_product_warehouse_stock` с уникальностью `(product_id, warehouse_id)`, FK и проверкой неотрицательного количества. `WarehouseStockService` является единым слоем чтения/изменения. Пользовательский read-model суммирует выбранные склады; административные показатели используют все активные склады. Документы и движения сохраняют `warehouse_id`, поэтому reverse operations возвращают товар на исходный склад. Bundle остаётся виртуальным SKU и вычисляется по компонентам в выбранных складах.

## 6. Удалённые legacy reads

Operational и current-stock reads переведены с `catalog_excel_products.stock` и `erp_component_inventory.physical_stock` в analytics, shared catalog, out-of-stock, category guards, delete/archive guards, exports, receipts, supplies, inventory, sales/reversal, WB matching/recovery, product cards/API и bundles. Полная классификация по найденным местам приведена в `docs/multiwarehouse-final-audit.md`.

## 7. Оставшиеся legacy reads

Оставлены только migration/backfill verification, diagnostic reconciliation и test-fixture compatibility bridge. Они не участвуют в runtime business decisions и перечислены с обоснованием в `docs/multiwarehouse-final-audit.md`. Legacy-колонки физически сохранены для rollback/совместимости.

## 8. Operational legacy writes

Новых operational writes в `catalog_excel_products.stock` и `erp_component_inventory.physical_stock` нет. Продажи, приходы (включая старый `/receipts/create`), Excel-приходы, поставки, списания, инвентаризация, restore/recovery, Bitrix/WB compatibility, component confirmation, transfer и отмены проходят через warehouse-aware service layer. Старые UI формы передают `warehouse_id` явно; исторический default Удельной применяется только в совместимых legacy-вызовах.

## 9. Full test results

- Backend full suite после синхронизации с актуальным `origin/main`: **1985 tests, OK, 14 skipped**, 307.714 s.
- Targeted suites после исправления финальных read-model опечаток и merge: component inventory 16/16, bundles 21/21, Excel export 7/7, order strap replacement 19/19, multiwarehouse Stage 5 23/23.
- Runtime DDL gate и migration tests пройдены; `git diff --check` и Python compile выполняются повторно перед публикацией.

## 10. Browser/frontend results

- Typecheck, e2e typecheck, lint — passed.
- Vitest — 39/39.
- Product Playwright — 11/11.
- Полный Playwright/accessibility/regression набор после merge в последовательном режиме — **50/50 passed**, 48.3 s. Параллельный запуск ранее воспроизвёл конкуренцию тестовой SQLite (49/50), поэтому итоговый gate выполнен с `--workers=1`.
- Real-copy browser QA: все 12 маршрутов HTTP 200; selector с одним/двумя складами, persistence, search, in/out-of-stock, pagination, создание третьего нулевого невыбранного склада только на copy, transfer round-trip, Escape и mobile overflow — passed.

## 11. Production-copy rehearsal

Rehearsal выполнен на свежей SQLite online-backup копии `/opt/clock-erp/instance/catalog.db`; production оригинал не изменялся. Пройдены preflight, deploy-time migration, повторный verify/idempotency/schema parity, startup, HTTP/browser smoke, reconciliation, integrity и performance. Внешних записей в Bitrix/МойСклад не выполнялось, network egress rehearsal равен нулю. После сохранения обезличенных JSON/screenshots временная БД-копия с production-данными удалена.

## 12. Real counts

- Products: **4547**.
- Physical components: **0**.
- Bundles: **0**.
- Warehouse stock rows: **4547** сразу после migration; **4548** после reversible transfer smoke, создавшего нулевую строку Гонконга.
- Historical backfill rows/backfilled/missing: batch 3313/3313/0; manual operations 1270/1270/0; receipt operations 439/439/0; receipt rows 439/439/0; Excel receipts 1/1/0; Excel stock operations 1672/1672/0; movements 1953/1953/0 после smoke; inventory sessions 72/72/0; receipts 28/28/0; sale items 119/119/0; writeoffs 2/2/0; component events 0/0/0.

## 13. OLD stock total

`SUM(catalog_excel_products.stock)` на свежей production copy: **464099.0**.

## 14. NEW Удельная total

`SUM(erp_product_warehouse_stock.quantity)` для Удельной: **464099.0**.

## 15. Гонконг total

После initial migration и после обратимого transfer round-trip: **0.0**.

## 16. Per-product reconciliation

Сравнено **4547** product_id; mismatch **0**, unexpected products **0**. Global OLD == NEW Удельная. `PRAGMA quick_check = ok`, FK violations = 0.

## 17. Migration hashes

- Migration ID: `2026-09-23-multiwarehouse-stock-v1`.
- Migration source checksum в проверенном наборе: `543eaef2951555900e18d3a5a3a818f874cb84be345de449c22b807e5f1db8ff`.
- OLD hash: `742e5c39b288c18aca5706c08bd104a0c0b68eda028bd5448a688dc3cc6e7118`.
- NEW Удельная hash: `742e5c39b288c18aca5706c08bd104a0c0b68eda028bd5448a688dc3cc6e7118`.

## 18. Performance

DB median после browser smoke: product list 11.689 ms; in stock 10.995 ms; out of stock 10.916 ms; search 3.902 ms; pagination 11.672 ms; warehouse totals 0.395 ms; bundle query 0.007 ms. Browser routes: products 443.793 ms; search 383.777; in stock 327.222; out of stock 705.283; pagination 425.704; receipts 70.899; sales 362.787; inventory 129.970; writeoffs 409.721; analytics 408.726; mobile products 405.083. Безопасный pre-change production HTTP baseline не снимался; отсутствие N+1 защищено query-count тестом.

## 19. QA screenshots

Семь screenshots и машинные результаты находятся в `QA_EVIDENCE/multiwarehouse-rehearsal/`: products/Удельная, selector, search по двум складам, out-of-stock, третий склад, transfer dialog, mobile. Полные цифры: `results.json`; reconciliation: `reconciliation.json`.

## 20. Backup/rollback runbook

Точный порядок maintenance/stop, manual safety backup, migration, verification, start, health check, smoke, rollback criteria и restore проверенного backup: `docs/multiwarehouse-production-runbook.md`. Rollback выполняется восстановлением backup, а не ручным удалением warehouse rows.

## 21. Известные ограничения

Production copy не содержит компонентов и bundles, поэтому real-data counts для них нулевые; lifecycle, concurrency и warehouse calculations покрыты изолированными regression tests. WB не превращался в отдельный склад: существующая подтверждённая семантика Удельной сохранена. Production baseline latency до изменений не снимался. Финальный production rollout потребует отдельной команды и manual safety backup по runbook.

## 22. Git status и deploy gate

После финального report commit/push статус ветки `agent/multiwarehouse` проверяется как clean; точный HEAD фиксируется при сдаче. Draft PR: https://github.com/otve4ai/clock-erp/pull/596. Production deploy, production migration, restart сервиса и PR merge **не выполнялись**.
