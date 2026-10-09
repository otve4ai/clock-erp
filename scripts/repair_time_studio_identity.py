"""Repair the verified Time Studio 20 mm identity, without changing inventory."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.catalog_db import CatalogDatabase
from app.clients.bitrix_catalog import BitrixCatalogReadOnlyClient
from app.services.audit_journal import AuditJournal
from app.services.bitrix_catalog_importer import BitrixCatalogImporter
from app.services.bitrix_erp_product_sync import create_database_backup


def repair(database, client, erp_id=10619, apply=False, backup_root=None):
    sources = [client.get_product(identity) for identity in ("243767", "244358")]
    for source, identity, article in zip(sources, ("243767", "244358"), ("TSB-18mm", "TSB-20mm")):
        if not source or source.get("external_product_id") != identity or source.get("external_sku") != article:
            raise ValueError("Live Bitrix identities differ from the verified repair plan")

    def inspect(connection):
        rows = connection.execute(
            "SELECT * FROM catalog_excel_products WHERE active = 1 AND "
            "(id = ? OR bitrix_external_product_id IN ('243767', '244358') "
            "OR lower(trim(excel_article)) IN ('tsb-18mm', 'tsb-20mm'))", (erp_id,),
        ).fetchall()
        if len(rows) != 1 or rows[0]["id"] != erp_id or rows[0]["excel_article"] != "TSB-20mm":
            raise ValueError("ERP cards differ from the verified repair plan")
        card = rows[0]
        if card["source_key"] not in ("bitrix:243767", "bitrix:244358"):
            raise ValueError("Unexpected ERP source key; manual review required")
        if connection.execute(
            "SELECT id FROM catalog_excel_products WHERE source_key = 'bitrix:244358' AND id <> ?",
            (erp_id,),
        ).fetchone():
            raise ValueError("The target ERP source key is already occupied")
        cached = connection.execute(
            "SELECT * FROM catalog_products WHERE id = ?", (card["bitrix_catalog_product_id"],),
        ).fetchone()
        if cached and connection.execute(
            "SELECT id FROM catalog_excel_products WHERE bitrix_catalog_product_id = ? AND id <> ?",
            (cached["id"], erp_id),
        ).fetchone():
            raise ValueError("Other ERP cards share the cached product; manual review required")
        if (cached and cached["external_source"] == "bitrix" and cached["article"] == "TSB-20mm"
                and cached["external_product_id"] == "244358"
                and card["bitrix_external_product_id"] == "244358"
                and card["bitrix_xml_id"] == sources[1].get("external_xml_id")):
            return card, card["source_key"] == "bitrix:244358"
        if (not cached or cached["external_source"] != "bitrix"
                or cached["external_product_id"] != "243767" or cached["article"] != "TSB-20mm"
                or card["bitrix_external_product_id"] != "243767" or card["bitrix_xml_id"] != "243767"):
            raise ValueError("Stored identities differ from the verified repair plan")
        return card, False

    connection = database.connect()
    try:
        card, done = inspect(connection)
        result = {"erp_id": erp_id, "stock": card["stock"], "from_bitrix_id": card["bitrix_external_product_id"],
                  "from_source_key": card["source_key"], "to_source_key": "bitrix:244358",
                  "to_bitrix_id": "244358", "status": "already_repaired" if done else "preview"}
    finally:
        connection.close()
    if done or not apply:
        return result
    if not backup_root:
        raise ValueError("An explicit backup directory is required")
    backup = create_database_backup(database, backup_root)
    if not backup:
        raise ValueError("Backup was not created")
    with database.transaction() as connection:
        card, done = inspect(connection)
        if done:
            return dict(result, status="already_repaired", backup=str(backup))
        before = dict(card)
        # Refresh both cache records and relink ERP in the same transaction.
        imported = BitrixCatalogImporter(database)._apply_products(connection, sources, "full_sync")
        if imported.get("conflicts") or any(item.get("status") == "conflict" for item in imported["items"]):
            raise ValueError("Ambiguous catalog identities; repair rolled back")
        target = connection.execute(
            "SELECT id FROM catalog_products WHERE external_source = 'bitrix' AND external_product_id = '244358'"
        ).fetchone()
        if target is None:
            raise ValueError("The 20 mm catalog record was not created")
        connection.execute(
            "UPDATE catalog_excel_products SET bitrix_external_product_id = '244358', "
            "bitrix_xml_id = ?, bitrix_catalog_product_id = ?, source_key = 'bitrix:244358' WHERE id = ?",
            (sources[1].get("external_xml_id"), target["id"], erp_id),
        )
        after = dict(connection.execute("SELECT * FROM catalog_excel_products WHERE id = ?", (erp_id,)).fetchone())
        changed = {key for key in before if before[key] != after[key]}
        if changed - {"bitrix_external_product_id", "bitrix_xml_id", "bitrix_catalog_product_id", "source_key"}:
            raise ValueError("Unexpected ERP card changes; repair rolled back")
        AuditJournal(database).record(
            "product", erp_id, "updated", card["excel_name_raw"], card["excel_article"],
            before={key: before[key] for key in changed}, after={key: after[key] for key in changed},
            source="bitrix_identity_repair", connection=connection,
        )
        return dict(result, status="repaired", stock=after["stock"], backup=str(backup))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--backup-dir")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    import app.config  # Load the application's normal configuration; never print it.
    client = BitrixCatalogReadOnlyClient(os.getenv("BITRIX_CATALOG_URL", ""), os.getenv("BITRIX_CATALOG_TOKEN"))
    result = repair(CatalogDatabase(args.database), client, apply=args.apply, backup_root=args.backup_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
