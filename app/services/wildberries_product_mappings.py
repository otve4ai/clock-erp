"""Explicit, variant-scoped WB links. No fuzzy matching or runtime DDL."""

import json


def variant_key(product):
    if product.get('source') != 'wildberries':
        return None
    nm_id = str(product.get('nm_id') or product.get('nmId') or product.get('nmID') or '').strip()
    chrt_id = str(product.get('chrt_id') or product.get('chrtId') or '').strip()
    if not nm_id:
        return None
    if chrt_id:
        return json.dumps([nm_id, 'chrt', chrt_id], separators=(',', ':'))
    barcodes = sorted(set(str(value).strip() for value in
                          [product.get('barcode'), product.get('sku')] + list(product.get('skus') or [])
                          if value is not None and str(value).strip()))
    if barcodes:
        return json.dumps([nm_id, 'skus', barcodes], separators=(',', ':'))
    # nmID/article alone cannot distinguish sizes; keep such choices order-local.
    return None


def remembered_products(connection, products):
    keys = sorted(set(key for key in map(variant_key, products) if key))
    result = {}
    for offset in range(0, len(keys), 400):
        chunk = keys[offset:offset + 400]
        rows = connection.execute(
            'SELECT identity_key,product_id FROM erp_wb_product_mappings WHERE identity_key IN (' +
            ','.join('?' for _ in chunk) + ')', chunk,
        ).fetchall()
        result.update((row['identity_key'], row['product_id']) for row in rows)
    return [result.get(variant_key(product)) for product in products]


def remember_product(connection, product, product_id, now):
    key = variant_key(product)
    if key is None:
        return False
    cursor = connection.execute(
        'UPDATE erp_wb_product_mappings SET product_id=?,updated_at=? WHERE identity_key=?',
        (int(product_id), now, key),
    )
    if not cursor.rowcount:
        connection.execute(
            'INSERT INTO erp_wb_product_mappings (identity_key,product_id,created_at,updated_at) VALUES (?,?,?,?)',
            (key, int(product_id), now, now),
        )
    return True


def forget_product(connection, product):
    key = variant_key(product)
    if key:
        connection.execute('DELETE FROM erp_wb_product_mappings WHERE identity_key=?', (key,))
