#!/usr/bin/env python3
"""Dry-run by default; restore mail presentation from read-only IMAP originals."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.mail import MailStore, MailTransport, SecretBox, safe_error
from app.services.mail_html_repair import repair_messages
from scripts.mail_worker import load_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-file")
    parser.add_argument("--database")
    parser.add_argument("--attachments")
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", help="Required for --apply; keep outside the web root")
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir")
    if not 1 <= args.limit <= 1000 or args.after_id < 0:
        parser.error("--limit must be 1..1000 and --after-id must be nonnegative")
    if args.environment_file:
        load_environment(args.environment_file)
    database = Path(args.database or os.getenv("ERP_MAIL_DATABASE") or ROOT / "instance/mail.db")
    store = MailStore(database, args.attachments or os.getenv("ERP_MAIL_ATTACHMENT_ROOT") or ROOT / "instance/mail-attachments")
    account = store.account(include_disabled=False)
    if not account:
        raise RuntimeError("No enabled mailbox")
    if args.apply:
        directory = Path(args.backup_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix="mail-html-", dir=str(directory))) / "mail-before.db"
        # SQLite CLI supports the production Python 3.6 runtime too.
        commands = ".timeout 10000\n.backup '{}'\n".format(str(target).replace("'", "''"))
        subprocess.run(["sqlite3", str(database)], input=commands.encode("utf-8"), check=True)
        os.chmod(str(target), 0o600)
        checked = subprocess.check_output(["sqlite3", str(target), "PRAGMA quick_check;"])
        if checked.strip() != b"ok":
            raise RuntimeError("Backup integrity check failed")
        print("BACKUP_OK=" + str(target), flush=True)
    client = MailTransport(account, SecretBox().decrypt(account["encrypted_password"])).imap()
    try:
        result = repair_messages(store, client, apply=args.apply, after_id=args.after_id, limit=args.limit)
        result["mode"] = "apply" if args.apply else "dry-run"
        print(json.dumps(result, sort_keys=True))
    finally:
        try:
            client.logout()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("MAIL_HTML_REPAIR_FAILED=" + safe_error(error), file=sys.stderr)
        sys.exit(1)
