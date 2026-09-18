#!/usr/bin/env python3
"""Preview or apply conservative canonical ERP category assignments."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.catalog_db import CatalogDatabase  # noqa: E402
from app.services.product_category_backfill import ProductCategoryBackfill  # noqa: E402


def main(arguments=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", help="SQLite catalog path")
    parser.add_argument("--output", help="Optional JSON report path")
    parser.add_argument("--backup-dir", help="Backup directory required for apply")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-digest")
    args = parser.parse_args(arguments)
    if args.apply and not args.confirm_plan_digest:
        parser.error("--apply requires --confirm-plan-digest from a fresh preview")
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir")

    service = ProductCategoryBackfill(
        CatalogDatabase(args.database, cache_initialization=False)
        if args.database else None
    )
    report = (
        service.apply(args.confirm_plan_digest, args.backup_dir)
        if args.apply else service.preview()
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
