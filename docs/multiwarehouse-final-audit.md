# Финальный аудит источников остатка

## Каноническая модель

Актуальный физический остаток хранится только в `erp_product_warehouse_stock` с ключом `(product_id, warehouse_id)`. Прямые и обратные операции используют `warehouse_id` документа/движения. Виртуальный bundle не имеет независимого остатка: доступность вычисляется по компонентам отдельно на каждом выбранном складе, затем складывается.

## Удалённые operational/analytics reads

На canonical warehouse stock переведены: product list/read model и карточки; search/filter/sort/pagination; shared catalog и out-of-stock; business analytics; category/delete/archive guards; product/Excel exports; receipts и recovery; sales/returns/cancellations; supplies/writeoffs/inventory; WB matching/recovery; bundle availability; component inventory; dashboard/API payloads. UI-поля `stock` и `physical_stock` в этих read models — вычисленные projections из выбранных складов, а не чтение одноимённых legacy-колонок.

Семантика аналитики фиксирована так:

- пользовательские списки, stock filters, stock KPI, bundle availability и экспорт — `USER SELECTED WAREHOUSES`;
- административные warehouse totals, запрет архивации непустого склада и глобальные integrity/reconciliation показатели — `ALL ACTIVE WAREHOUSES`;
- обратная операция — склад исходного документа, независимо от текущего выбора пользователя.

## Разрешённые legacy reads

- `app/multiwarehouse_migration.py`: migration/compatibility (D) — строит исходный snapshot, backfill и hash для доказательства `OLD == NEW Удельная`.
- `app/component_inventory_migration.py` и schema manifests: migration/compatibility (D) — описывают старые физические колонки и rollback-compatible contract.
- `app/services/numeric_brand_repair.py`: historical/diagnostic (C/D) — snapshot данных при repair, не решение о текущем остатке и не складская запись.
- `scripts/audit_receipt_integrity.py`: historical/diagnostic (C) — проверяет согласованность старых данных, не обслуживает runtime операции.
- `scripts/run_backend_tests.py`: test-only compatibility (D) — legacy fixture bridge включается только при `ERP_TEST_MODE=1` в изолированной временной БД; production код его не устанавливает.
- templates/JavaScript могут обращаться к JSON-ключам `stock`/`physical_stock`; эти ключи заполнены warehouse-aware service/read model и не являются чтением legacy DB columns.

Operational legacy reads класса A и analytics reads класса B отсутствуют. Dead operational paths удалены или переведены.

## Writes

Production business services не обновляют `catalog_excel_products.stock` и `erp_component_inventory.physical_stock`. Записи выполняются через warehouse-aware primitives (`set_balance`, `increase`, `decrease`, transfer) внутри транзакций. Оставшиеся записи legacy-полей существуют только в deploy-time migration/compatibility коде и test-only fixture bridge.

## WB и внешние интеграции

Отдельный WB-склад не создаётся. Существующая семантика WB/Bitrix/FBS остаётся привязана к default `Удельная`; возврат/отказ/отмена используют склад сохранённого документа. Сумма всех складов и `Гонконг` как неявный target не используются.
