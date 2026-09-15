"""Conservative preview and backfill for canonical ERP product taxonomy."""

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.catalog_db import CatalogDatabase
from app.services.audit_journal import AuditJournal
from app.services.inventory_lock import assert_no_active_inventory
from app.services.shared_catalog import normalized_name


def _text(value):
    return " ".join(str(value or "").split())


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProductCategoryBackfill:
    """Resolve legacy category text to existing ERP taxonomy IDs only."""

    def __init__(self, database=None):
        self.database = database or CatalogDatabase(cache_initialization=True)

    @staticmethod
    def _source_values(row, fields):
        return {
            field: _text(row.get(field))
            for field in fields
            if _text(row.get(field))
        }

    @staticmethod
    def _candidate_by_name(rows, key):
        return [dict(row) for row in rows if row["normalized_name"] == key]

    def _resolve_brand(self, row, brands):
        if row.get("brand_id") is not None and row.get("canonical_brand"):
            return {
                "id": int(row["brand_id"]),
                "name": row["canonical_brand"],
            }, "existing_canonical_brand", False
        sources = self._source_values(
            row, ("excel_brand", "bitrix_brand", "catalog_brand")
        )
        keys = {normalized_name(value) for value in sources.values()}
        if not sources:
            return None, "brand_missing", False
        if len(keys) != 1:
            return None, "brand_sources_conflict", True
        candidates = self._candidate_by_name(brands, next(iter(keys)))
        if len(candidates) == 1:
            return candidates[0], "exact_existing_erp_brand", False
        if not candidates:
            return None, "brand_not_found", True
        return None, "brand_name_ambiguous", True

    def _resolve_category(self, row, categories, relations, brand):
        sources = self._source_values(
            row,
            ("excel_category", "bitrix_category", "primary_category_name"),
        )
        keys = {normalized_name(value) for value in sources.values()}
        if not sources:
            return None, "category_missing", False
        if len(keys) != 1:
            return None, "category_sources_conflict", True
        candidates = self._candidate_by_name(categories, next(iter(keys)))
        if len(candidates) == 1:
            return candidates[0], "unique_normalized_category", False
        if not candidates:
            return None, "category_not_found", True
        if brand:
            brand_id = int(brand["id"])
            scoped = [
                item for item in candidates
                if int(item["brand_id"]) == brand_id
                or (brand_id, int(item["id"])) in relations
            ]
            if len(scoped) == 1:
                return scoped[0], "brand_scoped_duplicate_category", False
        return None, "category_name_ambiguous", True

    def _build(self, connection):
        rows = [dict(row) for row in connection.execute(
            "SELECT p.*, eb.name AS canonical_brand, cp.brand AS catalog_brand, "
            "cc.name AS primary_category_name "
            "FROM catalog_excel_products p "
            "LEFT JOIN erp_brands eb ON eb.id = p.brand_id AND eb.active = 1 "
            "LEFT JOIN catalog_products cp ON cp.id = p.bitrix_catalog_product_id "
            "LEFT JOIN catalog_categories cc ON cc.id = cp.primary_category_id "
            "WHERE p.active = 1 AND p.category_id IS NULL "
            "AND trim(COALESCE(p.excel_category, '')) <> '' ORDER BY p.id"
        ).fetchall()]
        brands = connection.execute(
            "SELECT id, name, normalized_name FROM erp_brands "
            "WHERE active = 1 ORDER BY id"
        ).fetchall()
        categories = connection.execute(
            "SELECT id, brand_id, name, normalized_name FROM erp_categories "
            "WHERE active = 1 ORDER BY id"
        ).fetchall()
        relations = {
            (int(row["brand_id"]), int(row["category_id"]))
            for row in connection.execute(
                "SELECT brand_id, category_id FROM erp_brand_categories"
            ).fetchall()
        }
        items = []
        for row in rows:
            brand, brand_reason, brand_ambiguous = self._resolve_brand(row, brands)
            category, category_reason, category_ambiguous = self._resolve_category(
                row, categories, relations, brand
            )
            automatic = category is not None and not category_ambiguous
            reason = category_reason
            if automatic and brand:
                reason += "+" + brand_reason
            elif automatic and brand_ambiguous:
                reason += "+category_only_brand_unresolved"
            item = {
                "product_id": int(row["id"]),
                "name": _text(row.get("excel_name_raw")),
                "excel_category": _text(row.get("excel_category")),
                "bitrix_category": _text(row.get("bitrix_category")),
                "primary_category_name": _text(row.get("primary_category_name")),
                "current_brand_id": row.get("brand_id"),
                "proposed_brand_id": int(brand["id"]) if brand else None,
                "erp_brand_name": brand["name"] if brand else "",
                "proposed_category_id": int(category["id"]) if category else None,
                "erp_category_name": category["name"] if category else "",
                "reason": reason,
                "brand_reason": brand_reason,
                "confidence": "high" if automatic else "ambiguous",
                "ambiguous": not automatic,
                "action": (
                    "assign_category_and_brand"
                    if automatic and brand else
                    "assign_category"
                    if automatic else
                    "requires_review"
                ),
            }
            items.append(item)

        current_uncategorized = int(connection.execute(
            "SELECT COUNT(*) FROM catalog_excel_products "
            "WHERE active = 1 AND category_id IS NULL"
        ).fetchone()[0])
        automatic_count = sum(not item["ambiguous"] for item in items)
        summary = {
            "eligible_legacy_products": len(items),
            "automatic_matches": automatic_count,
            "ambiguous": len(items) - automatic_count,
            "brand_assignments": sum(
                item["action"] == "assign_category_and_brand" for item in items
            ),
            "current_uncategorized": current_uncategorized,
            "remaining_uncategorized_after_apply": (
                current_uncategorized - automatic_count
            ),
        }
        digest_payload = [{key: value for key, value in item.items()
                           if key not in {"confidence"}} for item in items]
        return {
            "mode": "preview",
            "writes_performed": 0,
            "summary": summary,
            "items": items,
            "automatic": [item for item in items if not item["ambiguous"]],
            "ambiguous": [item for item in items if item["ambiguous"]],
            "plan_digest": hashlib.sha256(json.dumps(
                digest_payload, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")).hexdigest(),
        }

    def preview(self):
        self.database.initialize()
        with self.database.connect() as connection:
            return self._build(connection)

    def backup(self, backup_dir=None):
        source = Path(self.database.path)
        target_dir = Path(backup_dir) if backup_dir else source.parent / "backups"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "{}-categories-{}.bak".format(
            source.name, datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        with sqlite3.connect(str(source)) as source_connection:
            if hasattr(source_connection, "backup"):
                with sqlite3.connect(str(target)) as target_connection:
                    source_connection.backup(target_connection)
            else:  # Python 3.6 compatibility on production.
                escaped_target = str(target).replace("'", "''")
                subprocess.run(
                    ["sqlite3", str(source)],
                    input=".timeout 10000\n.backup '{}'\n".format(escaped_target),
                    universal_newlines=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
        with sqlite3.connect(str(target)) as backup_connection:
            if backup_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
        return str(target)

    def apply(self, expected_digest, backup_dir=None):
        if not expected_digest:
            raise ValueError("Preview plan digest is required")
        self.database.initialize()
        with self.database.connect() as connection:
            assert_no_active_inventory(connection)
            preview = self._build(connection)
        if preview["plan_digest"] != expected_digest:
            raise RuntimeError("Category backfill preview is stale; run preview again")
        backup_path = self.backup(backup_dir)
        applied = []
        with self.database.transaction() as connection:
            assert_no_active_inventory(connection)
            plan = self._build(connection)
            if plan["plan_digest"] != expected_digest:
                raise RuntimeError(
                    "Catalog changed after backup; category backfill was rolled back"
                )
            for item in plan["automatic"]:
                before = connection.execute(
                    "SELECT id, excel_name_raw, brand_id, category_id, stock "
                    "FROM catalog_excel_products WHERE id = ? AND active = 1",
                    (item["product_id"],),
                ).fetchone()
                if before is None or before["category_id"] is not None:
                    continue
                now = _now()
                connection.execute(
                    "UPDATE catalog_excel_products SET category_id = ?, "
                    "brand_id = CASE WHEN brand_id IS NULL THEN ? ELSE brand_id END, "
                    "updated_at = ? WHERE id = ? AND category_id IS NULL",
                    (
                        item["proposed_category_id"],
                        item["proposed_brand_id"],
                        now,
                        item["product_id"],
                    ),
                )
                if item["proposed_brand_id"] is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO erp_brand_categories "
                        "(brand_id, category_id, created_at) VALUES (?, ?, ?)",
                        (
                            item["proposed_brand_id"],
                            item["proposed_category_id"],
                            now,
                        ),
                    )
                AuditJournal(self.database).record(
                    "product", item["product_id"], "updated",
                    before["excel_name_raw"],
                    before={
                        "brand": "",
                        "category": "",
                    },
                    after={
                        "brand": item["erp_brand_name"],
                        "category": item["erp_category_name"],
                    },
                    metadata={
                        "source": "canonical_category_backfill",
                        "reason": item["reason"],
                        "confidence": item["confidence"],
                        "brand_id": item["proposed_brand_id"],
                        "category_id": item["proposed_category_id"],
                    },
                    actor_type="system",
                    actor_name="Category backfill",
                    occurred_at=now,
                    source="category_backfill",
                    connection=connection,
                )
                applied.append({
                    "product_id": item["product_id"],
                    "brand_id": item["proposed_brand_id"],
                    "category_id": item["proposed_category_id"],
                    "stock": before["stock"],
                })
        result = dict(preview)
        result.update({
            "mode": "apply",
            "writes_performed": len(applied),
            "backup_path": backup_path,
            "applied": applied,
        })
        return result
