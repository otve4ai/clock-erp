"""An administrator opts a body into one mandatory, order-specific strap."""
from datetime import datetime, timezone

from app.catalog_db import CatalogDatabase
from app.services.audit_journal import AuditJournal
from app.services.shared_catalog import catalog_category_is_strap


def requires_strap(connection, product_id):
    return connection.execute(
        "SELECT 1 FROM erp_required_straps WHERE product_id=?", (product_id,)
    ).fetchone() is not None


def sale_components(connection, product_id, quantity, strap_id=None):
    required = requires_strap(connection, product_id)
    if not required:
        if strap_id not in (None, ""):
            raise ValueError("У этого товара не включён признак «Требуется ремешок».")
        return None
    try:
        strap_id = int(strap_id)
    except (TypeError, ValueError):
        raise ValueError("Выберите ремешок для корпуса часов.")
    if strap_id == int(product_id):
        raise ValueError("Корпус и ремешок должны быть разными товарами.")
    body = connection.execute(
        "SELECT p.brand_id,COALESCE(b.name,p.excel_brand,'') AS brand "
        "FROM catalog_excel_products p LEFT JOIN erp_brands b ON b.id=p.brand_id "
        "WHERE p.id=?", (product_id,),
    ).fetchone()
    strap = connection.execute(
        "SELECT p.brand_id,COALESCE(b.name,p.excel_brand,'') AS brand,"
        "COALESCE(c.name,p.excel_category,'') AS category "
        "FROM catalog_excel_products p LEFT JOIN erp_brands b ON b.id=p.brand_id "
        "LEFT JOIN erp_categories c ON c.id=p.category_id "
        "WHERE p.id=? AND p.active=1 AND p.deleted_at IS NULL", (strap_id,),
    ).fetchone()
    if strap is None or not catalog_category_is_strap(strap["category"]):
        raise ValueError("Выберите активный товар из категории ремешков.")
    body_brand = str((body or {})["brand"] or "").strip() if body else ""
    strap_brand = str(strap["brand"] or "").strip()
    same_brand = bool(
        body and body_brand and strap_brand and (
            (
                body["brand_id"] is not None
                and strap["brand_id"] is not None
                and int(body["brand_id"]) == int(strap["brand_id"])
            )
            or body_brand.casefold() == strap_brand.casefold()
        )
    )
    if not same_brand:
        if not body_brand:
            raise ValueError(
                "У корпуса не указан бренд. Добавьте бренд в карточке товара."
            )
        raise ValueError(
            "Выберите ремешок бренда «{}».".format(body_brand)
        )
    if requires_strap(connection, strap_id) or connection.execute(
        "SELECT 1 FROM erp_product_bundles WHERE product_id IN (?,?)",
        (product_id, strap_id),
    ).fetchone():
        raise ValueError("Корпус и ремешок должны быть отдельными складскими товарами.")
    return [(int(product_id), quantity), (strap_id, quantity)]


class RequiredStraps:
    def __init__(self, database=None):
        self.database = database or CatalogDatabase()

    def product_ids(self):
        with self.database.connect() as connection:
            return {int(row[0]) for row in connection.execute(
                "SELECT product_id FROM erp_required_straps"
            )}

    def configure(self, product_id, enabled, actor=None):
        if not isinstance(enabled, bool):
            raise ValueError("Укажите, требуется ли ремешок.")
        with self.database.transaction() as connection:
            product = connection.execute(
                "SELECT id FROM catalog_excel_products WHERE id=? AND active=1",
                (product_id,),
            ).fetchone()
            if product is None:
                raise ValueError("Товар отсутствует или архивирован.")
            if enabled and connection.execute(
                "SELECT 1 FROM erp_product_bundles WHERE product_id=?", (product_id,)
            ).fetchone():
                raise ValueError("У товара уже настроен фиксированный состав.")
            before = requires_strap(connection, product_id)
            if before == enabled:
                return enabled
            if enabled:
                connection.execute(
                    "INSERT INTO erp_required_straps(product_id,updated_at) VALUES (?,?)",
                    (product_id, datetime.now(timezone.utc).isoformat()),
                )
            else:
                connection.execute("DELETE FROM erp_required_straps WHERE product_id=?", (product_id,))
            AuditJournal(self.database).record(
                "product", str(product_id), "updated", "Требуется ремешок", "erp",
                before={"requires_strap": before}, after={"requires_strap": enabled},
                actor_id=(actor or {}).get("actor_id"),
                actor_name=(actor or {}).get("actor_name"), connection=connection,
            )
        return enabled
