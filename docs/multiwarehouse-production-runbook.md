# Production runbook: мультисклад

Этот runbook применяется только после одобрения и merge Pull Request в `main` и отдельной команды Максима `ОКЕЙ, РАЗВОРАЧИВАЙ`. До этой команды production не изменяется.

## Условия запуска

- локальная и production-копии находятся на `main`, целевой commit совпадает с `origin/main`;
- локальный и production `git status` чисты;
- обязательные backend/frontend/browser проверки и rehearsal на свежей копии production БД завершились успешно;
- доступно достаточно места для двух полных копий `instance/catalog.db`;
- известен предыдущий commit и текущий release symlink;
- Bitrix и МойСклад не используются для тестовых записей.

## 1. Maintenance и остановка

1. Объявить окно обслуживания и запретить новые складские операции.
2. На сервере проверить `systemctl is-active clock-erp`, текущий commit, свободное место и чистый `git status` в `/opt/clock-erp`.
3. Остановить сервис: `systemctl stop clock-erp`.
4. Подтвердить `systemctl is-active clock-erp` = `inactive` и отсутствие процессов, удерживающих `instance/catalog.db`.

## 2. Обязательный ручной safety backup

Создать каталог с UTC timestamp вне release-каталога, права `0700`. С остановленным сервисом выполнить SQLite online backup:

```bash
sqlite3 /opt/clock-erp/instance/catalog.db ".backup '/opt/clock-erp-backups/<UTC>-multiwarehouse-manual/catalog-before.db'"
chmod 600 /opt/clock-erp-backups/<UTC>-multiwarehouse-manual/catalog-before.db
sqlite3 /opt/clock-erp-backups/<UTC>-multiwarehouse-manual/catalog-before.db "PRAGMA quick_check;"
sha256sum /opt/clock-erp-backups/<UTC>-multiwarehouse-manual/catalog-before.db > /opt/clock-erp-backups/<UTC>-multiwarehouse-manual/SHA256SUMS
```

Продолжать только при `quick_check = ok`, существующем непустом файле и успешном `sha256sum -c`. Сохранить абсолютный путь, размер и SHA-256 в журнале развёртывания. Не удалять backup до окончания согласованного retention.

## 3. Migration

Штатный путь — запустить из чистого локального `main` существующий `scripts/deploy.sh`: он делает отдельный preflight/rehearsal, останавливает сервис, создаёт собственный rollback backup, применяет deploy-time migrations и автоматически восстанавливает backup при ошибке. Ручной safety backup выше обязателен дополнительно.

Для ручной диагностики, но не вместо deploy script:

```bash
cd /opt/clock-erp
python3 scripts/migration_preflight.py preflight --database instance/catalog.db --source-root . --app-commit <commit> --service-stopped --report /opt/clock-erp-backups/<UTC>-multiwarehouse-manual/preflight.json
python3 scripts/migration_preflight.py apply --database instance/catalog.db --source-root . --app-commit <commit> --service-stopped --report /opt/clock-erp-backups/<UTC>-multiwarehouse-manual/apply.json
```

Нельзя запускать Flask worker с правом менять схему: runtime только валидирует migration ledger и schema contract.

## 4. Verification до старта

```bash
sqlite3 /opt/clock-erp/instance/catalog.db "PRAGMA quick_check; PRAGMA foreign_key_check;"
cd /opt/clock-erp
python3 scripts/migration_preflight.py verify --database instance/catalog.db --source-root . --app-commit <commit> --service-stopped --report /opt/clock-erp-backups/<UTC>-multiwarehouse-manual/verify.json
```

Проверить: ledger `applied`, schema checksum совпадает, FK-нарушений нет; `Удельная` и `Гонконг` активны; для каждого применимого товара legacy snapshot равен initial `Удельная`; общий итог совпадает; initial `Гонконг = 0`; historical `warehouse_id` заполнены. Любое неожиданное несовпадение — rollback без запуска сервиса.

## 5. Start

Запустить `systemctl start clock-erp`, затем проверить `systemctl is-active clock-erp` и последние записи `journalctl -u clock-erp --since '<start UTC>'`. Не выполнять повторную миграцию из worker.

## 6. Health check

Оба локальных endpoint должны ответить HTTP 200:

```bash
curl --fail --silent --show-error http://127.0.0.1:5000/register >/dev/null
curl --fail --silent --show-error http://127.0.0.1:5000/login >/dev/null
```

## 7. Smoke

Под read-only/admin тестовой сессией проверить `/app/products`, выбор `Удельная`, выбор `Удельная + Гонконг`, поиск, in/out of stock, pagination, breakdown, receipts, sales, writeoffs, inventory и analytics. Одну заранее согласованную складскую операцию выполнять только при отдельном разрешении; проверить audit и обратную операцию на том же складе.

## 8. Критерии rollback

Rollback обязателен при: ошибке migration/verification; несовпадении per-product или global stock; ненулевом initial `Гонконг`; FK/integrity ошибке; невозможности старта; HTTP не 200; 5xx на складских страницах; неверном складе прямой/обратной операции; отрицательном остатке; серьёзной деградации или потере истории.

## 9. Rollback

1. Немедленно запретить операции и остановить `clock-erp`.
2. Сохранить failed DB рядом с backup для анализа, не раскрывая данные.
3. Проверить ручной backup через `sha256sum -c` и `PRAGMA quick_check`.
4. Атомарно восстановить `/opt/clock-erp/instance/catalog.db` из проверенного `catalog-before.db` с прежними owner/mode.
5. Вернуть предыдущий release/commit штатным механизмом deploy script; не откручивать warehouse rows вручную.
6. Проверить `quick_check`, запустить сервис, проверить status, journal и оба HTTP 200 endpoint.
7. Зафиксировать причину, failed DB path, восстановленный commit и backup hash.
