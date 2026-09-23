"""Persistent employee checks for each distinct out-of-stock cycle."""

from datetime import datetime, timezone

from app.catalog_db import CatalogDatabase
from app.services.audit_journal import AuditJournal


PLATFORMS = ("ziiiro", "wildberries", "tictactoy")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class OutOfStockChecks:
    def __init__(self, database=None):
        self.database = database or CatalogDatabase(cache_initialization=True)

    def sync(self, connection=None):
        """Close replenished cycles and start exactly one cycle for new outages."""
        self.database.initialize()
        if connection is None:
            with self.database.transaction() as own_connection:
                self._sync(own_connection)
            return
        self._sync(connection)

    @staticmethod
    def _sync(connection):
        from app.services.excel_product_catalog import warehouse_stock_cte
        now = utc_now()
        warehouse_ids = [int(row[0]) for row in connection.execute(
            "SELECT id FROM erp_warehouses WHERE is_active=1 ORDER BY id"
        ).fetchall()]
        stock_cte, stock_parameters = warehouse_stock_cte(warehouse_ids)
        replenished = connection.execute(
            stock_cte + "SELECT p.id FROM catalog_excel_products p "
            "JOIN warehouse_stock_view s ON s.product_id=p.id "
            "WHERE s.quantity > 0 OR p.active = 0",
            stock_parameters,
        ).fetchall()
        replenished_ids = [int(row["id"]) for row in replenished]
        if replenished_ids:
            connection.execute(
                "UPDATE erp_out_of_stock_cycles SET ended_at = ?, updated_at = ? "
                "WHERE ended_at IS NULL AND product_id IN ({})".format(
                    ",".join("?" for _ in replenished_ids)
                ),
                [now, now] + replenished_ids,
            )
        missing = connection.execute(
            stock_cte + "SELECT p.id FROM catalog_excel_products p "
            "JOIN warehouse_stock_view s ON s.product_id=p.id "
            "WHERE p.active = 1 AND s.quantity <= 0 AND NOT EXISTS ("
            "SELECT 1 FROM erp_out_of_stock_cycles c "
            "WHERE c.product_id = p.id AND c.ended_at IS NULL)",
            stock_parameters,
        ).fetchall()
        for row in missing:
            connection.execute(
                "INSERT INTO erp_out_of_stock_cycles "
                "(product_id, started_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (row["id"], now, now, now),
            )
            cycle_id = connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            connection.executemany(
                "INSERT INTO erp_out_of_stock_checks "
                "(cycle_id, platform, checked, changed_at) VALUES (?, ?, 0, ?)",
                [(cycle_id, platform, now) for platform in PLATFORMS],
            )

    def current_for_products(self, product_ids):
        ids = sorted({int(value) for value in product_ids})
        if not ids:
            return {}
        self.sync()
        placeholders = ", ".join("?" for _ in ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT c.id AS cycle_id, c.product_id, c.started_at, "
                "k.platform, k.checked, k.changed_at, k.changed_by "
                "FROM erp_out_of_stock_cycles c "
                "JOIN erp_out_of_stock_checks k ON k.cycle_id = c.id "
                "WHERE c.ended_at IS NULL AND c.product_id IN ({})"
                .format(placeholders),
                ids,
            ).fetchall()
        result = {}
        for row in rows:
            item = result.setdefault(row["product_id"], {
                "cycle_id": row["cycle_id"],
                "started_at": row["started_at"],
                "checks": {},
            })
            item["checks"][row["platform"]] = {
                "checked": bool(row["checked"]),
                "changed_at": row["changed_at"],
                "changed_by": row["changed_by"] or "",
            }
        for item in result.values():
            checked_count = sum(
                1 for value in item["checks"].values() if value["checked"]
            )
            item["state"] = (
                "unchecked" if checked_count == 0
                else "complete" if checked_count == len(PLATFORMS)
                else "partial"
            )
        return result

    def set_check(self, product_id, platform, checked, actor_id="", actor_name=""):
        from app.services.excel_product_catalog import warehouse_stock_cte
        if platform not in PLATFORMS:
            raise ValueError("Неизвестная площадка.")
        product_id = int(product_id)
        checked = bool(checked)
        self.database.initialize()
        with self.database.transaction() as connection:
            self._sync(connection)
            warehouse_ids = [int(item[0]) for item in connection.execute(
                "SELECT id FROM erp_warehouses WHERE is_active=1 ORDER BY id"
            ).fetchall()]
            stock_cte, stock_parameters = warehouse_stock_cte(warehouse_ids)
            product = connection.execute(
                stock_cte + "SELECT p.id, p.excel_name_raw, p.excel_article, "
                "s.quantity AS stock FROM catalog_excel_products p "
                "JOIN warehouse_stock_view s ON s.product_id=p.id "
                "WHERE p.id = ? AND p.active = 1",
                stock_parameters + [product_id],
            ).fetchone()
            if product is None:
                raise ValueError("Товар не найден.")
            if float(product["stock"] or 0) > 0:
                raise ValueError("Товар снова в наличии; проверка уже не актуальна.")
            row = connection.execute(
                "SELECT c.id, k.checked FROM erp_out_of_stock_cycles c "
                "JOIN erp_out_of_stock_checks k ON k.cycle_id = c.id "
                "WHERE c.product_id = ? AND c.ended_at IS NULL AND k.platform = ?",
                (product_id, platform),
            ).fetchone()
            if row is None:
                raise ValueError("Цикл проверки не найден.")
            before = bool(row["checked"])
            if before != checked:
                now = utc_now()
                connection.execute(
                    "UPDATE erp_out_of_stock_checks SET checked = ?, "
                    "changed_at = ?, changed_by = ? "
                    "WHERE cycle_id = ? AND platform = ?",
                    (int(checked), now, actor_name or actor_id or None,
                     row["id"], platform),
                )
                connection.execute(
                    "UPDATE erp_out_of_stock_cycles SET updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                field = "check_{}".format(platform)
                AuditJournal(self.database).record(
                    "product", product_id, "updated",
                    product["excel_name_raw"], product["excel_article"] or "",
                    before={field: before}, after={field: checked},
                    metadata={"cycle_id": row["id"], "platform": platform},
                    actor_id=actor_id, actor_name=actor_name,
                    actor_type="user", connection=connection,
                )
        return self.current_for_products([product_id]).get(product_id)
