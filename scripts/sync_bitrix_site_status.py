#!/usr/bin/env python3
"""Synchronize only Bitrix ACTIVE flags with the ERP product catalog."""

import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.catalog_db import CatalogDatabase  # noqa: E402
from app.clients.bitrix_catalog import BitrixCatalogReadOnlyClient  # noqa: E402
from app.services.bitrix_site_status_sync import BitrixSiteStatusSync  # noqa: E402


def main():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    service = BitrixSiteStatusSync(
        database=CatalogDatabase(),
        client=BitrixCatalogReadOnlyClient(
            export_url=os.getenv("BITRIX_CATALOG_URL", ""),
            token=os.getenv("BITRIX_CATALOG_TOKEN"),
            max_retries=int(os.getenv("BITRIX_API_MAX_RETRIES", "3")),
        ),
    )
    try:
        result = service.run()
    except Exception as error:
        print(
            "Bitrix site-status sync failed: {}".format(type(error).__name__),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
