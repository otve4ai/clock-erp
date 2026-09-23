"""Transactional manual incoming documents, separate from supplier deliveries."""

import json
import uuid

from app.catalog_db import CatalogDatabase
from app.services.audit_journal import AuditJournal
from app.services.component_inventory import balance, write_balance
from app.services.inventory_lock import assert_products_unlocked
from app.services.receipt_inventory import ReceiptInventory, ReceiptInventoryError, utc_now


REASONS = {
    "stock_adjustment": "Корректировка остатка",
    "found": "Найденный товар",
    "manual_return": "Возврат на склад",
    "initial_stock": "Начальный остаток",
    "other": "Другое",
}


class ManualReceiptError(ReceiptInventoryError):
    pass


class ManualReceipts:
    def __init__(self, database=None):
        self.database = database or CatalogDatabase()

    def warehouses(self, include_inactive=False):
        self.database.initialize()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,code,name,active,is_default FROM erp_warehouses "
                + ("" if include_inactive else "WHERE active=1 ")
                + "ORDER BY is_default DESC,name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _warehouse(connection, warehouse_id, active=True):
        row = connection.execute(
            "SELECT * FROM erp_warehouses WHERE id=?" + (" AND active=1" if active else ""),
            (str(warehouse_id or ""),),
        ).fetchone()
        if row is None:
            raise ManualReceiptError("Выберите существующий активный склад.")
        return row

    @staticmethod
    def _reason(reason_code, comment):
        code = str(reason_code or "").strip()
        if code not in REASONS:
            raise ManualReceiptError("Выберите причину прихода.")
        if code == "other" and not str(comment or "").strip():
            raise ManualReceiptError("Для причины «Другое» укажите комментарий.")
        return code

    @staticmethod
    def _positions(items):
        try:
            return ReceiptInventory._prepare_positions(items)
        except ReceiptInventoryError as error:
            raise ManualReceiptError(str(error))

    @staticmethod
    def _next_number(connection):
        connection.execute(
            "INSERT OR IGNORE INTO erp_document_sequences(document_type,last_value) "
            "VALUES('manual_receipt',0)"
        )
        connection.execute(
            "UPDATE erp_document_sequences SET last_value=last_value+1 "
            "WHERE document_type='manual_receipt'"
        )
        value = connection.execute(
            "SELECT last_value FROM erp_document_sequences WHERE document_type='manual_receipt'"
        ).fetchone()[0]
        return "ПР-{:06d}".format(int(value))

    @staticmethod
    def _warehouse_balance(connection, warehouse, product_id, now):
        row = connection.execute(
            "SELECT quantity FROM erp_warehouse_stocks WHERE warehouse_id=? AND product_id=?",
            (warehouse["id"], int(product_id)),
        ).fetchone()
        if row is not None:
            return float(row[0] or 0)
        initial = balance(connection, product_id, require_initialized=False) if warehouse["is_default"] else 0.0
        connection.execute(
            "INSERT INTO erp_warehouse_stocks(warehouse_id,product_id,quantity,updated_at) "
            "VALUES(?,?,?,?)", (warehouse["id"], int(product_id), initial, now)
        )
        return initial

    @staticmethod
    def _set_warehouse_balance(connection, warehouse_id, product_id, quantity, now):
        connection.execute(
            "UPDATE erp_warehouse_stocks SET quantity=?,updated_at=? "
            "WHERE warehouse_id=? AND product_id=?",
            (quantity, now, warehouse_id, int(product_id)),
        )

    def create(self, warehouse_id, reason_code, items, comment="", actor="", key=""):
        prepared = self._positions(items)
        if len({item["product_id"] for item in prepared}) != len(prepared):
            raise ManualReceiptError("Один товар нельзя добавлять в приход дважды.")
        comment = str(comment or "").strip()
        reason_code = self._reason(reason_code, comment)
        now = utc_now()
        receipt_id = "manual-receipt:" + uuid.uuid4().hex
        idempotency = "manual-receipt:" + str(key).strip() if str(key or "").strip() else None
        self.database.initialize()
        with self.database.transaction() as connection:
            warehouse = self._warehouse(connection, warehouse_id)
            if idempotency:
                existing = connection.execute(
                    "SELECT id FROM erp_receipts WHERE tenant_id='default' AND idempotency_key=?",
                    (idempotency,),
                ).fetchone()
                if existing:
                    return self._get(connection, existing[0])
            products = ReceiptInventory._load_products(connection, prepared)
            number = self._next_number(connection)
            metadata = {
                "operation_type": "manual_receipt",
                "reason_label": REASONS[reason_code],
                "created_by": actor,
            }
            connection.execute(
                "INSERT INTO erp_receipts "
                "(id,tenant_id,number,comment,status,receipt_date,user_name,idempotency_key,"
                "metadata_json,created_at,updated_at,operation_type,warehouse_id,reason_code) "
                "VALUES(?,'default',?,?,'draft',?,?,?,?,?,?, 'manual_receipt',?,?)",
                (receipt_id, number, comment, now[:10], actor or None, idempotency,
                 json.dumps(metadata, ensure_ascii=False), now, now, warehouse["id"], reason_code),
            )
            ReceiptInventory._insert_items(connection, receipt_id, prepared, products, now)
            AuditJournal(self.database).record(
                "receipt", receipt_id, "created", "Приход #{}".format(number),
                after={"status": "draft", "operation_type": "manual_receipt", "warehouse": warehouse["name"]},
                metadata={"number": number, "reason": reason_code}, actor_id=actor,
                actor_name=actor, actor_type="user" if actor else "system",
                status="draft", connection=connection,
            )
            return self._get(connection, receipt_id)

    def update(self, receipt_id, warehouse_id, reason_code, items, comment="", actor=""):
        prepared = self._positions(items)
        if len({item["product_id"] for item in prepared}) != len(prepared):
            raise ManualReceiptError("Один товар нельзя добавлять в приход дважды.")
        comment = str(comment or "").strip()
        reason_code = self._reason(reason_code, comment)
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._row(connection, receipt_id, draft=True)
            warehouse = self._warehouse(connection, warehouse_id)
            products = ReceiptInventory._load_products(connection, prepared)
            connection.execute("UPDATE erp_receipt_items SET active=0 WHERE receipt_id=?", (receipt_id,))
            ReceiptInventory._insert_items(connection, receipt_id, prepared, products, now)
            connection.execute(
                "UPDATE erp_receipts SET warehouse_id=?,reason_code=?,comment=?,user_name=?,updated_at=? WHERE id=?",
                (warehouse["id"], reason_code, comment, actor or row["user_name"], now, receipt_id),
            )
            AuditJournal(self.database).record(
                "receipt", receipt_id, "updated", "Приход #{}".format(row["number"]),
                after={"status": "draft", "warehouse": warehouse["name"], "reason": reason_code},
                metadata={"number": row["number"]}, actor_id=actor, actor_name=actor,
                actor_type="user" if actor else "system", status="draft", connection=connection,
            )
            return self._get(connection, receipt_id)

    @staticmethod
    def _row(connection, receipt_id, draft=False):
        row = connection.execute(
            "SELECT * FROM erp_receipts WHERE id=? AND operation_type='manual_receipt'", (str(receipt_id),)
        ).fetchone()
        if row is None:
            raise ManualReceiptError("Приход не найден.")
        if draft and row["status"] != "draft":
            raise ManualReceiptError("Редактировать можно только черновик прихода.")
        return row

    def post(self, receipt_id, actor="", failure_hook=None):
        now = utc_now()
        with self.database.transaction() as connection:
            receipt = self._row(connection, receipt_id)
            if receipt["status"] == "posted":
                return self._get(connection, receipt_id)
            if receipt["status"] != "draft":
                raise ManualReceiptError("Отменённый приход нельзя провести повторно.")
            warehouse = self._warehouse(connection, receipt["warehouse_id"])
            items = connection.execute(
                "SELECT * FROM erp_receipt_items WHERE receipt_id=? AND active=1 ORDER BY id", (receipt_id,)
            ).fetchall()
            if not items:
                raise ManualReceiptError("Добавьте хотя бы один товар.")
            products = ReceiptInventory._load_products(
                connection, [{"product_id": item["product_id"]} for item in items]
            )
            assert_products_unlocked(connection, products, ManualReceiptError)
            for item in items:
                product_id = int(item["product_id"])
                quantity = float(item["quantity"])
                if not warehouse["is_default"]:
                    default_warehouse = connection.execute(
                        "SELECT * FROM erp_warehouses WHERE active=1 AND is_default=1"
                    ).fetchone()
                    if default_warehouse is None:
                        raise ManualReceiptError("Основной склад не настроен.")
                    self._warehouse_balance(connection, default_warehouse, product_id, now)
                warehouse_before = self._warehouse_balance(connection, warehouse, product_id, now)
                warehouse_after = warehouse_before + quantity
                total_before = balance(connection, product_id, ("receipt", receipt_id), False)
                total_after = total_before + quantity
                self._set_warehouse_balance(connection, warehouse["id"], product_id, warehouse_after, now)
                write_balance(connection, product_id, total_after, "manual_receipt", now, ("receipt", receipt_id))
                connection.execute(
                    "INSERT INTO catalog_stock_movements "
                    "(id,product_id,movement_type,quantity_delta,stock_before,stock_after,receipt_id,"
                    "receipt_item_id,idempotency_key,tenant_id,source_type,source_id,source_line_id,"
                    "operation_kind,source_number,source,user_name,comment,created_at,warehouse_id) "
                    "VALUES(?,?,'receipt',?,?,?,?,? ,?,'default','manual_receipt',?,?,'post',?,"
                    "'Приход',?,?,?,?)",
                    (uuid.uuid4().hex, product_id, quantity, total_before, total_after, receipt_id,
                     item["id"], "manual-receipt-post:{}:{}".format(receipt_id, item["id"]),
                     receipt_id, str(item["id"]), receipt["number"], actor or receipt["user_name"],
                     receipt["comment"], now, warehouse["id"]),
                )
            connection.execute(
                "UPDATE erp_receipts SET status='posted',posted_at=?,posted_by=?,updated_at=? "
                "WHERE id=? AND status='draft'", (now, actor or None, now, receipt_id)
            )
            if failure_hook:
                failure_hook(connection)
            AuditJournal(self.database).record(
                "receipt", receipt_id, "status_changed", "Приход #{}".format(receipt["number"]),
                before={"status": "draft"}, after={"status": "posted"},
                metadata={"number": receipt["number"]}, actor_id=actor, actor_name=actor,
                actor_type="user" if actor else "system", status="posted", connection=connection,
            )
            return self._get(connection, receipt_id)

    def cancel(self, receipt_id, actor="", failure_hook=None):
        now = utc_now()
        with self.database.transaction() as connection:
            receipt = self._row(connection, receipt_id)
            if receipt["status"] == "cancelled":
                return self._get(connection, receipt_id)
            if receipt["status"] != "posted":
                raise ManualReceiptError("Черновик прихода не отменяется: его можно удалить.")
            warehouse = self._warehouse(connection, receipt["warehouse_id"], active=False)
            items = connection.execute(
                "SELECT * FROM erp_receipt_items WHERE receipt_id=? AND active=1 ORDER BY id", (receipt_id,)
            ).fetchall()
            products = ReceiptInventory._load_products(
                connection, [{"product_id": item["product_id"]} for item in items],
                include_archived=True, document=("receipt", receipt_id),
            )
            totals = {}
            for item in items:
                totals[int(item["product_id"])] = totals.get(int(item["product_id"]), 0) + float(item["quantity"])
            assert_products_unlocked(connection, totals, ManualReceiptError)
            for product_id, quantity in totals.items():
                available = self._warehouse_balance(connection, warehouse, product_id, now)
                if available + 0.000001 < quantity:
                    raise ManualReceiptError(
                        "Невозможно отменить приход: на складе доступно {} ед., для отмены требуется {} ед.".format(
                            int(available) if available.is_integer() else available,
                            int(quantity) if quantity.is_integer() else quantity,
                        )
                    )
                total_before = balance(connection, product_id, ("receipt", receipt_id), False)
                if total_before + 0.000001 < quantity:
                    raise ManualReceiptError("Невозможно отменить приход: суммарного остатка недостаточно.")
                self._set_warehouse_balance(connection, warehouse["id"], product_id, available - quantity, now)
                write_balance(connection, product_id, total_before - quantity, "manual_receipt_cancel", now, ("receipt", receipt_id))
                connection.execute(
                    "INSERT INTO catalog_stock_movements "
                    "(id,product_id,movement_type,quantity_delta,stock_before,stock_after,receipt_id,"
                    "idempotency_key,tenant_id,source_type,source_id,source_line_id,operation_kind,"
                    "source_number,source,user_name,comment,created_at,warehouse_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, product_id, "cancellation", -quantity,
                     total_before, total_before - quantity, receipt_id,
                     "manual-receipt-cancel:{}:{}".format(receipt_id, product_id),
                     "default", "manual_receipt_reversal", receipt_id,
                     "product:{}".format(product_id), "cancel", receipt["number"],
                     "Приход", actor or None,
                     "Отмена прихода №{}".format(receipt["number"]), now, warehouse["id"]),
                )
            connection.execute(
                "UPDATE erp_receipts SET status='cancelled',cancelled_at=?,cancelled_by=?,updated_at=? WHERE id=?",
                (now, actor or None, now, receipt_id),
            )
            if failure_hook:
                failure_hook(connection)
            AuditJournal(self.database).record(
                "receipt", receipt_id, "cancelled", "Приход #{}".format(receipt["number"]),
                before={"status": "posted"}, after={"status": "cancelled"},
                metadata={"number": receipt["number"]}, actor_id=actor, actor_name=actor,
                actor_type="user" if actor else "system", status="cancelled", connection=connection,
            )
            return self._get(connection, receipt_id)

    def delete(self, receipt_id, actor=""):
        with self.database.transaction() as connection:
            receipt = self._row(connection, receipt_id, draft=True)
            connection.execute("DELETE FROM erp_receipt_items WHERE receipt_id=?", (receipt_id,))
            connection.execute("DELETE FROM erp_receipts WHERE id=?", (receipt_id,))
            AuditJournal(self.database).record(
                "receipt", receipt_id, "deleted", "Приход #{}".format(receipt["number"]),
                before={"status": "draft"}, after={"status": "deleted"},
                metadata={"number": receipt["number"]}, actor_id=actor, actor_name=actor,
                actor_type="user" if actor else "system", status="deleted", connection=connection,
            )
        return {"id": receipt_id, "deleted": True}

    def get(self, receipt_id):
        with self.database.connect() as connection:
            return self._get(connection, receipt_id)

    def list(self):
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT id FROM erp_receipts WHERE operation_type='manual_receipt' "
                "ORDER BY created_at DESC,id DESC"
            )]
            return [self._get(connection, receipt_id) for receipt_id in ids]

    @staticmethod
    def _get(connection, receipt_id):
        receipt = connection.execute(
            "SELECT r.*,w.name AS warehouse_name FROM erp_receipts r "
            "JOIN erp_warehouses w ON w.id=r.warehouse_id "
            "WHERE r.id=? AND r.operation_type='manual_receipt'", (str(receipt_id),)
        ).fetchone()
        if receipt is None:
            raise ManualReceiptError("Приход не найден.")
        result = dict(receipt)
        result["reason_label"] = REASONS.get(result["reason_code"], result["reason_code"] or "—")
        result["created_by"] = result.get("user_name") or ""
        result["items"] = []
        rows = connection.execute(
            "SELECT i.*,p.excel_name_raw AS name,COALESCE(p.excel_article,'') AS article,"
            "COALESCE(b.name,p.excel_brand,'') AS brand,COALESCE(c.name,p.excel_category,'') AS category "
            "FROM erp_receipt_items i JOIN catalog_excel_products p ON p.id=i.product_id "
            "LEFT JOIN erp_brands b ON b.id=p.brand_id LEFT JOIN erp_categories c ON c.id=p.category_id "
            "WHERE i.receipt_id=? AND i.active=1 ORDER BY i.id", (receipt_id,)
        ).fetchall()
        for row in rows:
            item = dict(row)
            stock = connection.execute(
                "SELECT quantity FROM erp_warehouse_stocks WHERE warehouse_id=? AND product_id=?",
                (receipt["warehouse_id"], row["product_id"]),
            ).fetchone()
            item["stock"] = float(stock[0] or 0) if stock else 0
            quantity = float(item["quantity"])
            if result["status"] == "draft":
                item["stock_before"] = item["stock"]
                item["stock_after"] = item["stock"] + quantity
            elif result["status"] == "posted":
                item["stock_before"] = item["stock"] - quantity
                item["stock_after"] = item["stock"]
            else:
                item["stock_before"] = item["stock"]
                item["stock_after"] = item["stock"] + quantity
            result["items"].append(item)
        result["position_count"] = len(result["items"])
        result["total_quantity"] = sum(float(item["quantity"]) for item in result["items"])
        return result
