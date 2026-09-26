"""Read Bitrix product activity and update only the ERP site-status field."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from app.catalog_db import CatalogDatabase
from app.services.excel_product_catalog import product_site_issue_sql

try:
    import fcntl
except ImportError:  # pragma: no cover - production uses Linux/fcntl.
    fcntl = None
    import msvcrt


RUN_MODE = "site_status_sync"


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SiteStatusSyncLock:
    """One lock shared by the UI action and the nightly systemd service."""

    def __init__(self, database=None, path=None):
        database = database or CatalogDatabase()
        self.path = Path(
            path
            or os.getenv("BITRIX_SITE_STATUS_LOCK_PATH")
            or database.path.parent / ".bitrix-site-status-sync.lock"
        )
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            if fcntl is not None:
                fcntl.flock(
                    self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            else:
                self.handle.seek(0)
                if self.handle.read(1) == "":
                    self.handle.write("0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError):
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self):
        if self.handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        else:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()
        self.handle = None


class BitrixSiteStatusSync:
    def __init__(self, database=None, client=None, lock=None):
        self.database = database or CatalogDatabase()
        self.client = client
        self.lock = lock or SiteStatusSyncLock(self.database)

    def summary(self):
        self.database.initialize()
        in_stock_inactive_sql = product_site_issue_sql("in_stock_inactive")
        out_of_stock_active_sql = product_site_issue_sql("out_of_stock_active")
        with self.database.connect() as connection:
            counts = connection.execute(
                "SELECT COUNT(*) AS positions, "
                "SUM(CASE WHEN trim(COALESCE(bitrix_external_product_id, '')) "
                "<> '' THEN 1 ELSE 0 END) AS linked, "
                "SUM(CASE WHEN " + in_stock_inactive_sql + " "
                "THEN 1 ELSE 0 END) AS in_stock_inactive, "
                "SUM(CASE WHEN " + out_of_stock_active_sql + " "
                "THEN 1 ELSE 0 END) AS out_of_stock_active, "
                "SUM(CASE WHEN stock > 0 AND "
                "trim(COALESCE(bitrix_external_product_id, '')) = '' "
                "THEN 1 ELSE 0 END) AS in_stock_unlinked, "
                "SUM(CASE WHEN trim(COALESCE(bitrix_external_product_id, '')) "
                "<> '' AND bitrix_active IS NULL THEN 1 ELSE 0 END) "
                "AS unknown_statuses "
                "FROM catalog_excel_products p WHERE active = 1"
            ).fetchone()
            run = connection.execute(
                "SELECT status, started_at, finished_at, products_received, "
                "products_updated, errors_count, error_summary, details_json "
                "FROM catalog_sync_runs WHERE mode = ? "
                "ORDER BY id DESC LIMIT 1",
                (RUN_MODE,),
            ).fetchone()
        result = {
            key: int(counts[key] or 0)
            for key in (
                "positions", "linked", "in_stock_inactive",
                "out_of_stock_active", "in_stock_unlinked",
                "unknown_statuses",
            )
        }
        result.update({
            "outcome": "unknown",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": "",
            "received": 0,
            "updated": 0,
            "automatic_schedule": "03:00",
        })
        if run is not None:
            try:
                details = json.loads(run["details_json"] or "{}")
            except (TypeError, ValueError):
                details = {}
            result.update({
                "outcome": (
                    "running" if run["status"] == "running"
                    else "success" if run["status"] == "success"
                    else "error"
                ),
                "last_attempt_at": run["started_at"],
                "last_success_at": (
                    run["finished_at"] if run["status"] == "success"
                    else details.get("previous_success_at")
                ),
                "last_error": run["error_summary"] or "",
                "received": int(run["products_received"] or 0),
                "updated": int(run["products_updated"] or 0),
            })
        result["mismatch_count"] = (
            result["in_stock_inactive"] + result["out_of_stock_active"]
        )
        result["has_data"] = bool(result["last_success_at"])
        return result

    def _create_run(self):
        previous = self.summary()
        with self.database.transaction() as connection:
            return connection.execute(
                "INSERT INTO catalog_sync_runs "
                "(mode, status, started_at, details_json) VALUES (?, ?, ?, ?)",
                (
                    RUN_MODE, "running", utc_now(),
                    json.dumps({
                        "previous_success_at": previous.get("last_success_at"),
                        "inventory_operations": 0,
                        "bitrix_writes": 0,
                    }, ensure_ascii=False),
                ),
            ).lastrowid

    def _finish_run(self, run_id, report):
        details = dict(report)
        details.update({"inventory_operations": 0, "bitrix_writes": 0})
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_sync_runs SET status='success', finished_at=?, "
                "pages_processed=?, products_received=?, products_updated=?, "
                "products_unchanged=?, errors_count=0, error_summary=NULL, "
                "details_json=? WHERE id=?",
                (
                    utc_now(), report["pages"], report["received"],
                    report["updated"], report["unchanged"],
                    json.dumps(details, ensure_ascii=False), run_id,
                ),
            )

    def _fail_run(self, run_id, error):
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT details_json FROM catalog_sync_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            try:
                details = json.loads(row["details_json"] or "{}")
            except (TypeError, ValueError):
                details = {}
            details.update({"inventory_operations": 0, "bitrix_writes": 0})
            connection.execute(
                "UPDATE catalog_sync_runs SET status='failed', finished_at=?, "
                "errors_count=1, error_summary=?, details_json=? WHERE id=?",
                (
                    utc_now(), type(error).__name__,
                    json.dumps(details, ensure_ascii=False), run_id,
                ),
            )

    def run(self, page_size=200):
        if self.client is None:
            raise RuntimeError("Bitrix client is required")
        if not self.lock.acquire():
            result = self.summary()
            result.update({"outcome": "running", "coalesced": True})
            return result
        started = time.monotonic()
        run_id = None
        try:
            run_id = self._create_run()
            statuses = {}
            pages = received = unknown_source_statuses = 0
            page = 1
            while True:
                payload = self.client.get_products_page(
                    page=page,
                    limit=max(1, min(int(page_size), 200)),
                    include_inactive=True,
                )
                pages += 1
                products = payload.get("products") or []
                received += len(products)
                for product in products:
                    product_id = str(
                        product.get("external_product_id") or ""
                    ).strip()
                    if not product_id:
                        continue
                    if not product.get("active_known", "active" in product):
                        unknown_source_statuses += 1
                        continue
                    statuses[product_id] = int(bool(product.get("active")))
                if not payload.get("has_more") or not products:
                    break
                page += 1

            updated = unchanged = missing_from_bitrix = 0
            with self.database.transaction() as connection:
                linked = connection.execute(
                    "SELECT id, bitrix_external_product_id, bitrix_active "
                    "FROM catalog_excel_products WHERE active = 1 AND "
                    "trim(COALESCE(bitrix_external_product_id, '')) <> ''"
                ).fetchall()
                for row in linked:
                    external_id = str(row["bitrix_external_product_id"]).strip()
                    if external_id not in statuses:
                        missing_from_bitrix += 1
                        continue
                    next_active = statuses[external_id]
                    if row["bitrix_active"] == next_active:
                        unchanged += 1
                        continue
                    connection.execute(
                        "UPDATE catalog_excel_products SET bitrix_active=? "
                        "WHERE id=?",
                        (next_active, row["id"]),
                    )
                    updated += 1
            report = {
                "run_id": run_id,
                "pages": pages,
                "received": received,
                "updated": updated,
                "unchanged": unchanged,
                "missing_from_bitrix": missing_from_bitrix,
                "unknown_source_statuses": unknown_source_statuses,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
            self._finish_run(run_id, report)
            result = self.summary()
            result.update(report)
            result["outcome"] = "success"
            return result
        except Exception as error:
            if run_id is not None:
                self._fail_run(run_id, error)
            raise
        finally:
            self.lock.release()
