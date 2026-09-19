# Бэкапы ERP

Production-данные находятся в `/opt/clock-erp/instance`. Штатный инструмент
`/usr/local/sbin/clock-erp-backup-retention` ежедневно в 03:17 (timezone сервера)
создаёт архив в `/opt/clock-erp-backups/daily` и сразу выполняет ротацию. Daily
хранит один валидный архив в сутки за последние семь календарных дней. Ручные
архивы создаются только кнопкой ERP в `/opt/clock-erp-backups/manual` и
автоматически не удаляются. Временных, pre-deploy и pre-restore архивов нет.
Оба потока используют общий `/opt/clock-erp-backups/.retention.lock`.

Архив содержит `.env` проекта и `instance/`. SQLite снимаются через online backup
API/CLI и проходят `PRAGMA quick_check`; JSON, локальные изображения и вложения из
`instance/` копируются вместе с каталогом. Не входят системный environment file,
systemd/nginx/cron/TLS/SSH, venv, Git-код и внешняя БД Bitrix. Копии лежат на том
же filesystem, поэтому независимая off-host защита всё ещё необходима.

Вкладка `/app/backups` доступна только роли `admin` (владелец). Она показывает
фактические filesystem, backup и Git-данные. Ручная кнопка вызывает тот же штатный
инструмент, проверяет место, архив, состав SQLite и `quick_check`, после чего
пишет закрытую metadata с backup ID, timestamp, типом, размером, commit/branch и
schema state. Старые архивы без metadata не считаются точками `код + данные`.

## Recovery V2

Recovery выполняет только установленный root-owned helper
`/usr/local/sbin/clock-erp-recovery`. Flask передаёт ему только созданный на
сервере `operation_id`; backup ID и commit повторно проверяются helper. Helper не
принимает shell-команды или filesystem paths. Состояния и безопасные журналы
лежат в `/opt/clock-erp-backups/recovery/{operations,logs}` с mode `0600`.

Совместимость определяется на backend по точному Git commit, версии Recovery
metadata, хешу `ops/recovery-schema-contract.json`, полному file manifest,
SQLite schema digest и обязательным таблицам. В UI доступны только ежедневные и
ручные архивы; архив без полной metadata не используется для восстановления.
Откат кода и полный restore также блокируются, если `requirements.txt` выбранной
версии отличается от декларации зависимостей текущего immutable release.

Выбранный архив безопасно распаковывается в закрытый staging с
запретом traversal, ссылок и special files. После сверки manifests включается
maintenance marker, останавливаются web service и фоновые writer timers, затем
`instance` меняется атомарным directory swap. `.env` из архива игнорируется.

Код запускается из immutable release `/opt/clock-erp-releases/<commit>` через
атомарный `/opt/clock-erp-current`. Source checkout `/opt/clock-erp` остаётся на
чистой `main`, поэтому rollback не использует detached HEAD, `reset --hard` или
удаление local changes. Full restore объединяет тот же data swap и точный release
commit из metadata. Исходный `instance` остаётся частью атомарного swap только
на время операции и удаляется после успешной проверки; отдельный backup при
restore не создаётся. При ошибке после swap выполняется одна попытка обратного
swap; повторная ошибка переводит операцию в `critical` и сохраняет диагностику.

## ERP работает

Владелец открывает «Бэкапы», выбирает разрешённое backend действие и вводит
различающийся текст подтверждения. Во время операции страница показывает
реальные этапы; обновление страницы не теряет `operation_id`. Недоступная кнопка
содержит конкретную причину блокировки.

## ERP не запускается

Используется тот же helper, без Flask и без путей от оператора:

```bash
/usr/local/sbin/clock-erp-recovery restore-data --backup-id <backup_id> --idempotency-key <unique-key>
/usr/local/sbin/clock-erp-recovery rollback-code --commit <40-char-commit> --idempotency-key <unique-key>
/usr/local/sbin/clock-erp-recovery restore-system --backup-id <backup_id> --idempotency-key <unique-key>
```

Helper сам блокирует неизвестные ID, неподтверждённые metadata/commit, dirty Git,
несовместимую schema, недостаток места и параллельную операцию.

## Recovery failed

Текущее безопасное состояние выводится командой:

```bash
/usr/local/sbin/clock-erp-recovery status <operation_id>
```

Технический журнал находится в
`/opt/clock-erp-backups/recovery/logs/<operation_id>.jsonl`. В нём нет содержимого
`.env`, токенов, паролей или stack traces. Статус `critical` означает, что
единственная automatic rollback попытка не завершилась; maintenance остаётся
включённым, повторные destructive команды не запускают и проводят ручную
диагностику по operation state и recovery log.
