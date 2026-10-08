"""Read-only stock projection shared by catalog lists and reporting.

One projected row per product: warehouse joins must never multiply SKU counts.
Company totals include transfers in transit; availability never does.
"""

from app.services.component_inventory import PHYSICAL_STOCK_SQL


class WarehouseStockScope:
    def __init__(self, connection, warehouse_id="all"):
        self.warehouse_id = warehouse_id
        self.predicate = "1=1"
        if warehouse_id == "all":
            self.available = "COALESCE((SELECT SUM(quantity) FROM erp_warehouse_stocks ws WHERE ws.product_id=p.id),0)"
            self.transit = "COALESCE((SELECT SUM(ti.quantity) FROM erp_stock_transfer_items ti JOIN erp_stock_transfers t ON t.id=ti.transfer_id WHERE ti.product_id=p.id AND t.status='in_transit'),0)"
            self.total = "({} + {})".format(self.available, self.transit)
        else:
            if not connection.execute(
                "SELECT 1 FROM erp_warehouses WHERE id=? AND active=1", (warehouse_id,)
            ).fetchone():
                raise ValueError("Склад не найден или отключён.")
            quoted = connection.execute("SELECT quote(?)", (warehouse_id,)).fetchone()[0]
            self.transit = "0"
            if warehouse_id == "default":
                self.available = PHYSICAL_STOCK_SQL
            else:
                self.available = "COALESCE((SELECT quantity FROM erp_warehouse_stocks ws WHERE ws.product_id=p.id AND ws.warehouse_id={}),0)".format(quoted)
                self.predicate = "(EXISTS(SELECT 1 FROM erp_warehouse_stocks ws WHERE ws.product_id=p.id AND ws.warehouse_id={0}) OR EXISTS(SELECT 1 FROM erp_stock_transfer_items ti JOIN erp_stock_transfers t ON t.id=ti.transfer_id WHERE ti.product_id=p.id AND t.to_warehouse_id={0} AND t.status='in_transit'))".format(quoted)
            self.total = self.available

        self.products = (
            "(SELECT p.id,p.active,p.brand_id,p.category_id,p.model,p.model_id,"
            "p.current_batch_id,p.source_key,p.excel_name_raw,p.excel_brand,"
            "p.excel_category,p.excel_article,p.cell,"
            "{total} AS stock,{available} AS available_stock,{transit} AS transit_stock "
            "FROM catalog_excel_products p WHERE {scope} AND "
            "(p.source_key LIKE 'bitrix:%' OR EXISTS(SELECT 1 FROM catalog_excel_batches batch "
            "WHERE batch.id=p.current_batch_id AND batch.status='active')))"
        ).format(total=self.total, available=self.available, transit=self.transit,
                 scope=self.predicate)

    def execute(self, connection, sql, parameters=()):
        """Expand the explicit reporting_products query source, never a writer.

        SQL and the marker are developer-owned; user values stay bound parameters.
        No temporary tables, schema writes, connection state or stock updates.
        """
        if not sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("Stock reports only support SELECT queries")
        return connection.execute(sql.replace("reporting_products", self.products), parameters)
