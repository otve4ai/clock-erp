#!/usr/bin/env python3
"""One bounded mailbox worker pass for cron/systemd."""

from __future__ import print_function

import argparse
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mail import MailStore, MailSynchronizer, SecretBox, safe_error


def sync_due(store, now=None):
    """Poll incoming mail every 15 minutes; explicit requests bypass the delay."""
    account = store.account(include_disabled=False)
    if not account:
        return False
    with store.connect() as connection:
        pending = connection.execute(
            "SELECT 1 FROM mail_sync_requests WHERE account_id=? AND state='pending' LIMIT 1",
            (account["id"],),
        ).fetchone()
    if pending:
        return True
    last_attempt = (account.get("updated_at") if account.get("last_sync_status") == "error"
                    else account.get("last_sync_at"))
    if not last_attempt:
        return True
    # MailStore writes UTC timestamps; retain Python 3.6 production compatibility.
    last_attempt = datetime.strptime(str(last_attempt)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last_attempt >= timedelta(minutes=15)


def load_environment(path):
    """Read protected service configuration without exporting or logging secrets."""
    path = Path(path)
    details = path.stat()
    if details.st_uid != 0 or stat.S_IMODE(details.st_mode) != 0o600:
        raise ValueError("Protected environment file required")
    from dotenv import dotenv_values
    values = dotenv_values(str(path), interpolate=False)
    for key in ("ERP_MAIL_DATABASE", "ERP_MAIL_ATTACHMENT_ROOT", "ERP_MAIL_SECRET_KEY",
                "CDEK_ACCOUNT", "CDEK_PASSWORD", "CDEK_CACHE_DIR",
                "CDEK_EMAIL_REMINDERS_ENABLED", "ORDERS_DATABASE_PATH"):
        if key in values and values[key] is not None:
            os.environ[key] = values[key]


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file")
    parser.add_argument("--database")
    parser.add_argument("--attachments")
    args = parser.parse_args()
    if args.environment_file:
        load_environment(args.environment_file)
    database = args.database or os.getenv("ERP_MAIL_DATABASE", "") or str(ROOT / "instance" / "mail.db")
    attachments = args.attachments or os.getenv("ERP_MAIL_ATTACHMENT_ROOT", "") or str(ROOT / "instance" / "mail-attachments")
    lock_path = ROOT / "instance" / ".mail-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("MAIL_WORKER=already_running")
            return 0
        store = MailStore(database, attachments)
        if not store.account(include_disabled=False):
            print("MAIL_WORKER=disconnected")
            return 0
        reminders = None
        if os.getenv("CDEK_EMAIL_REMINDERS_ENABLED") == "1":
            from app.services.cdek_delivery import CdekDelivery
            from app.services.cdek_reminders import CdekReminders
            from app.services.cdek_sales import group_sales
            from app.clients.cdek import CdekError
            from app.web import api_sales_records
            reminders = CdekReminders(CdekDelivery(), store)
            try:
                prepared = reminders.prepare(group_sales(api_sales_records()))
                print("CDEK_EMAIL queued={queued} skipped={skipped} errors={errors}".format(**prepared))
            except (CdekError, OSError):
                print("CDEK_EMAIL=prepare_failed", file=sys.stderr)
        worker = MailSynchronizer(store, SecretBox(), cdek_reminders=reminders)
        delivery = worker.deliver()
        sync = worker.sync() if sync_due(store) else {"messages": 0, "threads": 0}
        print("MAIL_WORKER=ok sent={} imported={} threads={}".format(delivery["sent"], sync["messages"], sync["threads"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("MAIL_WORKER=error message={}".format(safe_error(error)), file=sys.stderr)
        sys.exit(1)
