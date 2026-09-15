# Бэкапы ERP

Production-данные находятся в `/opt/clock-erp/instance`. Штатный инструмент
`/usr/local/sbin/clock-erp-backup-retention` ежедневно в 03:17 (timezone сервера)
создаёт архив в `/opt/clock-erp-backups/daily`; retention запускается каждый час
в :23. Daily хранит один валидный архив в сутки за последние семь календарных
дней, temporary — три дня, operational — 30 дней. Все потоки используют общий
`/opt/clock-erp-backups/.retention.lock`.

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

Restore и rollback через UI заблокированы до установки ограниченного privileged
helper и успешной репетиции на изолированной копии. Аварийное восстановление не
зависит от Flask: оператор проверяет архив, извлекает его в новый закрытый staging,
проверяет все SQLite read-only, останавливает все writers, создаёт safety backup,
атомарно заменяет `instance`, восстанавливает согласованный Git commit и только
после schema/service/HTTP/read-only бизнес-проверок возвращает фоновые процессы.
Текущие `.env` и системные настройки нельзя заменять автоматически.
