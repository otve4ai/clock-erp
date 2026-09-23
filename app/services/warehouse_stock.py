"""Canonical warehouse-aware physical stock service."""

from __future__ import print_function

import math
import uuid
from datetime import datetime, timezone

from app.catalog_db import CatalogDatabase
from app.multiwarehouse_migration import DEFAULT_WAREHOUSE_CODE


class WarehouseStockError(ValueError):
    pass


class InsufficientWarehouseStock(WarehouseStockError):
    def __init__(self, available):
        self.available = float(available)
        super().__init__("Недостаточно товара на выбранном складе.")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def quantity(value, allow_zero=True):
    if isinstance(value, bool):
        raise WarehouseStockError("Количество должно быть числом.")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise WarehouseStockError("Количество должно быть числом.")
    if not math.isfinite(result) or result < 0 or (not allow_zero and result == 0):
        raise WarehouseStockError("Количество должно быть положительным числом.")
    return result


def default_warehouse_id(connection):
    row = connection.execute(
        "SELECT id FROM erp_warehouses WHERE code=? AND is_active=1",
        (DEFAULT_WAREHOUSE_CODE,),
    ).fetchone()
    if row is None:
        raise WarehouseStockError("Склад по умолчанию не настроен.")
    return int(row[0])


def require_warehouse(connection, warehouse_id=None, allow_legacy_default=False):
    if warehouse_id in (None, ""):
        if not allow_legacy_default:
            raise WarehouseStockError("Выберите склад.")
        warehouse_id = default_warehouse_id(connection)
    try:
        warehouse_id = int(warehouse_id)
    except (TypeError, ValueError):
        raise WarehouseStockError("Склад не найден.")
    row = connection.execute(
        "SELECT id FROM erp_warehouses WHERE id=? AND is_active=1",
        (warehouse_id,),
    ).fetchone()
    if row is None:
        raise WarehouseStockError("Склад не найден или архивирован.")
    return warehouse_id


def get_balance(connection, product_id, warehouse_id=None,
                allow_legacy_default=False, require_initialized=True):
    warehouse_id = require_warehouse(
        connection, warehouse_id, allow_legacy_default=allow_legacy_default
    )
    row = connection.execute(
        "SELECT quantity,initialized_at FROM erp_product_warehouse_stock "
        "WHERE product_id=? AND warehouse_id=?",
        (int(product_id), warehouse_id),
    ).fetchone()
    if row is None:
        return 0.0
    if row[1] is None and require_initialized:
        raise WarehouseStockError(
            "Физический остаток компонента на складе не подтверждён."
        )
    return float(row[0] or 0)


def balances(connection, product_ids, warehouse_ids):
    product_ids = sorted({int(value) for value in product_ids})
    warehouse_ids = sorted({int(value) for value in warehouse_ids})
    if not product_ids or not warehouse_ids:
        return {}
    result = {(product_id, warehouse_id): 0.0
              for product_id in product_ids for warehouse_id in warehouse_ids}
    for product_offset in range(0, len(product_ids), 400):
        products = product_ids[product_offset:product_offset + 400]
        parameters = products + warehouse_ids
        rows = connection.execute(
            "SELECT product_id,warehouse_id,quantity FROM erp_product_warehouse_stock "
            "WHERE product_id IN ({}) AND warehouse_id IN ({})".format(
                ",".join("?" for _ in products),
                ",".join("?" for _ in warehouse_ids),
            ), parameters,
        ).fetchall()
        for row in rows:
            result[(int(row[0]), int(row[1]))] = float(row[2] or 0)
    return result


def selected_total(connection, product_id, warehouse_ids):
    warehouse_ids = sorted({int(value) for value in warehouse_ids})
    if not warehouse_ids:
        raise WarehouseStockError("Выберите хотя бы один склад.")
    row = connection.execute(
        "SELECT COALESCE(SUM(quantity),0) FROM erp_product_warehouse_stock "
        "WHERE product_id=? AND warehouse_id IN ({})".format(
            ",".join("?" for _ in warehouse_ids)
        ), [int(product_id)] + warehouse_ids,
    ).fetchone()
    return float(row[0] or 0)


def set_balance(connection, product_id, warehouse_id, value, timestamp=None,
                initialized=True):
    warehouse_id = require_warehouse(connection, warehouse_id)
    value = quantity(value)
    timestamp = timestamp or utc_now()
    product = connection.execute(
        "SELECT 1 FROM catalog_excel_products WHERE id=?", (int(product_id),)
    ).fetchone()
    if product is None:
        raise WarehouseStockError("Товар не найден.")
    connection.execute(
        "INSERT OR IGNORE INTO erp_product_warehouse_stock "
        "(product_id,warehouse_id,quantity,initialized_at,updated_at) "
        "VALUES (?,?,0,?,?)",
        (int(product_id), warehouse_id, timestamp if initialized else None, timestamp),
    )
    cursor = connection.execute(
        "UPDATE erp_product_warehouse_stock SET quantity=?,"
        "initialized_at=CASE WHEN ? THEN COALESCE(initialized_at,?) ELSE initialized_at END,"
        "updated_at=? WHERE product_id=? AND warehouse_id=?",
        (value, int(bool(initialized)), timestamp, timestamp,
         int(product_id), warehouse_id),
    )
    if cursor.rowcount != 1:
        raise WarehouseStockError("Не удалось изменить остаток.")
    return cursor


def increase(connection, product_id, warehouse_id, amount, timestamp=None):
    amount = quantity(amount, allow_zero=False)
    before = get_balance(connection, product_id, warehouse_id, require_initialized=False)
    set_balance(connection, product_id, warehouse_id, before + amount, timestamp)
    return before, before + amount


def decrease(connection, product_id, warehouse_id, amount, timestamp=None):
    warehouse_id = require_warehouse(connection, warehouse_id)
    amount = quantity(amount, allow_zero=False)
    timestamp = timestamp or utc_now()
    before = get_balance(connection, product_id, warehouse_id)
    cursor = connection.execute(
        "UPDATE erp_product_warehouse_stock SET quantity=quantity-?,updated_at=? "
        "WHERE product_id=? AND warehouse_id=? AND initialized_at IS NOT NULL "
        "AND quantity>=?",
        (amount, timestamp, int(product_id), warehouse_id, amount),
    )
    if cursor.rowcount != 1:
        raise InsufficientWarehouseStock(before)
    return before, before - amount


class WarehouseStockService:
    def __init__(self, database=None):
        self.database = database or CatalogDatabase()

    def list_warehouses(self, include_archived=False):
        self.database.initialize()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM erp_warehouses {} ORDER BY id".format(
                    "" if include_archived else "WHERE is_active=1"
                )
            ).fetchall()
            return [dict(row) for row in rows]

    def selected_warehouse_ids(self, user_id):
        """Return an always non-empty, active per-user warehouse selection."""
        user_id = str(user_id or "anonymous")
        self.database.initialize()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT w.id FROM erp_user_warehouse_preferences p "
                "JOIN erp_warehouses w ON w.id=p.warehouse_id "
                "WHERE p.user_id=? AND w.is_active=1 ORDER BY w.id",
                (user_id,),
            ).fetchall()
            if rows:
                return [int(row[0]) for row in rows]
            return [default_warehouse_id(connection)]

    def set_selected_warehouses(self, user_id, warehouse_ids):
        user_id = str(user_id or "anonymous")
        try:
            warehouse_ids = sorted({int(value) for value in warehouse_ids})
        except (TypeError, ValueError):
            raise WarehouseStockError("Некорректный список складов.")
        if not warehouse_ids:
            raise WarehouseStockError("Выберите хотя бы один склад.")
        self.database.initialize()
        with self.database.transaction() as connection:
            active = {
                int(row[0]) for row in connection.execute(
                    "SELECT id FROM erp_warehouses WHERE is_active=1 AND id IN ({})".format(
                        ",".join("?" for _ in warehouse_ids)
                    ), warehouse_ids,
                ).fetchall()
            }
            if active != set(warehouse_ids):
                raise WarehouseStockError("Один из складов не найден или архивирован.")
            connection.execute(
                "DELETE FROM erp_user_warehouse_preferences WHERE user_id=?",
                (user_id,),
            )
            timestamp = utc_now()
            connection.executemany(
                "INSERT INTO erp_user_warehouse_preferences"
                "(user_id,warehouse_id,selected_at) VALUES (?,?,?)",
                [(user_id, warehouse_id, timestamp) for warehouse_id in warehouse_ids],
            )
        return self.selected_warehouse_ids(user_id)

    def list_for_user(self, user_id):
        selected = set(self.selected_warehouse_ids(user_id))
        self.database.initialize()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT w.id,w.code,w.name,w.is_active,"
                "COALESCE(SUM(CASE WHEN p.active=1 AND b.product_id IS NULL "
                "THEN s.quantity ELSE 0 END),0) AS total "
                "FROM erp_warehouses w "
                "LEFT JOIN erp_product_warehouse_stock s ON s.warehouse_id=w.id "
                "LEFT JOIN catalog_excel_products p ON p.id=s.product_id "
                "LEFT JOIN erp_product_bundles b ON b.product_id=p.id "
                "WHERE w.is_active=1 GROUP BY w.id ORDER BY w.id"
            ).fetchall()
        return [{**dict(row), "selected": int(row["id"]) in selected} for row in rows]

    def rename_warehouse(self, warehouse_id, name):
        name = " ".join(str(name or "").split())
        if not name:
            raise WarehouseStockError("Название склада обязательно.")
        self.database.initialize()
        with self.database.transaction() as connection:
            warehouse_id = require_warehouse(connection, warehouse_id)
            try:
                connection.execute(
                    "UPDATE erp_warehouses SET name=?,normalized_name=?,updated_at=? WHERE id=?",
                    (name, name.casefold(), utc_now(), warehouse_id),
                )
            except Exception as error:
                if "UNIQUE" in str(error).upper():
                    raise WarehouseStockError("Склад с таким названием уже существует.")
                raise
            return dict(connection.execute(
                "SELECT * FROM erp_warehouses WHERE id=?", (warehouse_id,)
            ).fetchone())

    def archive_warehouse(self, warehouse_id):
        self.database.initialize()
        with self.database.transaction() as connection:
            warehouse_id = require_warehouse(connection, warehouse_id)
            if warehouse_id == default_warehouse_id(connection):
                raise WarehouseStockError("Склад по умолчанию нельзя архивировать.")
            nonempty = connection.execute(
                "SELECT 1 FROM erp_product_warehouse_stock "
                "WHERE warehouse_id=? AND abs(quantity)>0.000001 LIMIT 1",
                (warehouse_id,),
            ).fetchone()
            if nonempty is not None:
                raise WarehouseStockError("Архивировать можно только пустой склад.")
            connection.execute(
                "UPDATE erp_warehouses SET is_active=0,updated_at=? WHERE id=?",
                (utc_now(), warehouse_id),
            )
            connection.execute(
                "DELETE FROM erp_user_warehouse_preferences WHERE warehouse_id=?",
                (warehouse_id,),
            )
        return True

    def create_warehouse(self, name, code=None):
        name = " ".join(str(name or "").split())
        if not name:
            raise WarehouseStockError("Название склада обязательно.")
        normalized = name.casefold()
        code = str(code or normalized.replace(" ", "-")).strip().casefold()
        if not code:
            raise WarehouseStockError("Код склада обязателен.")
        timestamp = utc_now()
        self.database.initialize()
        with self.database.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO erp_warehouses(code,name,normalized_name,is_active,created_at,updated_at) "
                    "VALUES (?,?,?,1,?,?)",
                    (code, name, normalized, timestamp, timestamp),
                )
            except Exception as error:
                if "UNIQUE" in str(error).upper():
                    raise WarehouseStockError("Склад с таким названием или кодом уже существует.")
                raise
            warehouse_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
            return dict(connection.execute(
                "SELECT * FROM erp_warehouses WHERE id=?", (warehouse_id,)
            ).fetchone())

    def get_balance(self, product_id, warehouse_id):
        self.database.initialize()
        with self.database.connect() as connection:
            return get_balance(connection, product_id, warehouse_id)

    def selected_total(self, product_id, warehouse_ids):
        self.database.initialize()
        with self.database.connect() as connection:
            return selected_total(connection, product_id, warehouse_ids)

    def increase(self, product_id, warehouse_id, amount):
        self.database.initialize()
        with self.database.transaction() as connection:
            return increase(connection, product_id, warehouse_id, amount)

    def decrease(self, product_id, warehouse_id, amount):
        self.database.initialize()
        with self.database.transaction() as connection:
            return decrease(connection, product_id, warehouse_id, amount)

    def transfer(self, product_id, from_warehouse_id, to_warehouse_id, amount,
                 actor=None, comment="", idempotency_key="", failure_hook=None):
        amount = quantity(amount, allow_zero=False)
        actor = actor or {}
        transfer_id = uuid.uuid4().hex
        timestamp = utc_now()
        self.database.initialize()
        with self.database.transaction() as connection:
            source = require_warehouse(connection, from_warehouse_id)
            target = require_warehouse(connection, to_warehouse_id)
            if source == target:
                raise WarehouseStockError("Склады перемещения должны отличаться.")
            if connection.execute(
                "SELECT 1 FROM erp_product_bundles WHERE product_id=?", (int(product_id),)
            ).fetchone() is not None:
                raise WarehouseStockError("Перемещайте физические компоненты комплекта.")
            if idempotency_key:
                existing = connection.execute(
                    "SELECT t.*,i.product_id,i.quantity FROM erp_stock_transfers t "
                    "JOIN erp_stock_transfer_items i ON i.transfer_id=t.id "
                    "WHERE t.idempotency_key=?",
                    (str(idempotency_key),),
                ).fetchone()
                if existing is not None:
                    if (
                        int(existing["from_warehouse_id"]) != source
                        or int(existing["to_warehouse_id"]) != target
                        or int(existing["product_id"]) != int(product_id)
                        or abs(float(existing["quantity"]) - amount) > 0.000001
                    ):
                        raise WarehouseStockError(
                            "Ключ операции уже использован для другого перемещения."
                        )
                    return dict(existing)
            before_source, after_source = decrease(
                connection, product_id, source, amount, timestamp
            )
            before_target, after_target = increase(
                connection, product_id, target, amount, timestamp
            )
            connection.execute(
                "INSERT INTO erp_stock_transfers "
                "(id,from_warehouse_id,to_warehouse_id,status,idempotency_key,"
                "actor_id,actor_name,comment,created_at) VALUES (?,?,?,'posted',?,?,?,?,?)",
                (transfer_id, source, target, str(idempotency_key or "") or None,
                 str(actor.get("actor_id") or "") or None,
                 str(actor.get("actor_name") or "") or None,
                 str(comment or ""), timestamp),
            )
            connection.execute(
                "INSERT INTO erp_stock_transfer_items(transfer_id,product_id,quantity) "
                "VALUES (?,?,?)", (transfer_id, int(product_id), amount)
            )
            movement_values = (
                (source, target, -amount, before_source, after_source, "transfer_out"),
                (target, source, amount, before_target, after_target, "transfer_in"),
            )
            for warehouse_id, counterparty, delta, before, after, operation in movement_values:
                connection.execute(
                    "INSERT INTO catalog_stock_movements "
                    "(id,product_id,movement_type,quantity_delta,stock_before,stock_after,"
                    "idempotency_key,source_type,source_id,operation_kind,source,user_name,"
                    "comment,created_at,warehouse_id,transfer_id,counterparty_warehouse_id) "
                    "VALUES (?,?,'manual_adjustment',?,?,?,?,'stock_transfer',?,?,"
                    "'Перемещение',?,?,?,?,?,?)",
                    (uuid.uuid4().hex, int(product_id), delta, before, after,
                     "{}:{}".format(idempotency_key, operation) if idempotency_key else None,
                     transfer_id, operation, actor.get("actor_name") or None,
                     str(comment or ""), timestamp, warehouse_id, transfer_id, counterparty),
                )
            if failure_hook:
                failure_hook(connection)
            return dict(connection.execute(
                "SELECT * FROM erp_stock_transfers WHERE id=?", (transfer_id,)
            ).fetchone())
