#!/usr/bin/env python3
"""Refresh delivery statuses only; never submit SMS messages."""
import argparse
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment-file", required=True)
    args = parser.parse_args()
    try:
        path = Path(args.environment_file)
        details = path.stat()
        if details.st_uid != 0 or stat.S_IMODE(details.st_mode) != 0o600:
            raise ValueError("Protected environment file required")
        from dotenv import dotenv_values
        values = dotenv_values(str(path), interpolate=False)
        for key in ("SMSBLISS_LOGIN", "SMSBLISS_PASSWORD", "SMSBLISS_API_BASE_URL",
                    "SMSBLISS_STATUS_QUEUE_NAME", "ERP_SMS_DATABASE"):
            if key in values and values[key] is not None:
                os.environ[key] = values[key]
        from app.clients.smsbliss import SmsBlissClient
        from app.services.sms import SmsService, SmsStore
        client = SmsBlissClient()
        if not client.configured:
            raise ValueError("SMS provider is not configured")
        database = os.environ.get("ERP_SMS_DATABASE", "").strip() or str(ROOT / "instance" / "sms.db")
        store = SmsStore(database)
        store.verify()
        result = SmsService(store, client).sync_statuses()
        print("checked={} updated={}".format(result["checked"], result["updated"]))
        return 0
    except Exception as error:
        # Never include provider responses or environment values in cron logs.
        print("SMS_STATUS_SYNC_FAILED: {}".format(type(error).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
