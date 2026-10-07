#!/usr/bin/env python3
"""One bounded mailbox worker pass for cron/systemd."""

from __future__ import print_function

import argparse
import fcntl
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mail import MailStore, MailSynchronizer, SecretBox, safe_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=os.getenv("ERP_MAIL_DATABASE", "") or str(ROOT / "instance" / "mail.db"))
    parser.add_argument("--attachments", default=os.getenv("ERP_MAIL_ATTACHMENT_ROOT", "") or str(ROOT / "instance" / "mail-attachments"))
    args = parser.parse_args()
    lock_path = ROOT / "instance" / ".mail-worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("MAIL_WORKER=already_running")
            return 0
        store = MailStore(args.database, args.attachments)
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
        sync = worker.sync()
        print("MAIL_WORKER=ok sent={} imported={} threads={}".format(delivery["sent"], sync["messages"], sync["threads"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print("MAIL_WORKER=error message={}".format(safe_error(error)), file=sys.stderr)
        sys.exit(1)
