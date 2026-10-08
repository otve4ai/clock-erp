# Восстановление HTML-писем

Статус: draft — реализовано и проверено локально; production ещё не обновлён.

Очистка HTML теперь удаляет содержимое style, script, title, template, iframe,
object и noscript. Ранее их текст сохранялся. Уже сохранённый CSS нельзя надёжно
отделить от текста, поэтому восстановление использует оригиналы из IMAP.

После отдельного разрешения на production сначала выполнить dry-run:

```sh
cd /opt/clock-erp-current
/opt/clock-erp/venv/bin/python scripts/repair_mail_html.py \
  --environment-file /etc/clock-erp/clock-erp.env --limit 200
```

Для записи добавить `--apply --backup-dir /opt/clock-erp-backups/mail-html-repair`.
Перед записью каждой партии создаётся SQLite-копия mail.db с проверкой quick_check.
При ошибке backup запись не начинается. Схема и настройки не меняются.

Оригиналы читаются через BODY.PEEK[] в read-only папке. UIDVALIDITY и Message-ID
должны совпасть. Отсутствующие оригиналы и несовпадения пропускаются. Обновляются
только html_body, text_body, snippet, external_images и краткий текст цепочки,
если восстановлено последнее письмо. Номера, статусы, прочитанность, ссылки,
получатели и вложения сохраняются; новые письма не создаются.

Отчёт содержит checked, changed, skipped, last_id. Следующую партию запускать с
`--after-id LAST_ID` до checked=0. Первый apply должен иметь тот же after-id, что
проверенный dry-run, а не начинаться после его last_id. Повтор партии безопасен:
уже исправленные письма не меняются. При сетевой ошибке повторить партию: часть
писем может быть уже сохранена. Пропуски требуют проверки наличия оригиналов;
локальные письма не удалять. Тела писем и учётные данные в отчёт не выводятся.

Проверки: `python -m unittest tests.test_mail tests.test_mail_html_repair`.
