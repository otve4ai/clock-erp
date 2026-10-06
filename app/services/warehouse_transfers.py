"""Local, transactional transfer documents. Transit is owned, never sellable."""
import json
import math
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

from app.catalog_db import CatalogDatabase
from app.services.component_inventory import balance, write_balance
from app.services.inventory_lock import assert_products_unlocked
from app.services.audit_journal import AuditJournal


class TransferError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class WarehouseTransfers:
    def __init__(self, database=None):
        self.database = database or CatalogDatabase()

    @contextmanager
    def _transaction(self, connection=None):
        if connection is not None:
            yield connection
        else:
            with self.database.transaction() as owned:
                yield owned

    def create(self, source, destination, items, key, actor='', comment='', connection=None):
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise TransferError('Укажите ключ операции.')
        if not isinstance(source, str) or not isinstance(destination, str) or not source or not destination or source == destination:
            raise TransferError('Выберите два разных склада.')
        if not isinstance(items, list) or not items or len(items) > 1000:
            raise TransferError('Добавьте товары в перемещение.')
        if len(str(comment or '')) > 2000:
            raise TransferError('Комментарий не должен превышать 2000 символов.')
        prepared = {}
        for item in items:
            if not isinstance(item, dict) or isinstance(item.get('quantity'), bool):
                raise TransferError('Некорректная строка перемещения.')
            try:
                if isinstance(item.get('product_id'), bool) or not str(item.get('product_id')).isdigit():
                    raise ValueError()
                pid, quantity = int(item['product_id']), float(item['quantity'])
            except (KeyError, TypeError, ValueError, OverflowError):
                raise TransferError('Укажите товар и количество.')
            if not 0 < pid <= 9223372036854775807:
                raise TransferError('Некорректный ID товара.')
            if not math.isfinite(quantity) or not 0 < quantity <= 2147483647 or quantity != int(quantity):
                raise TransferError('Количество должно быть целым положительным числом.')
            if pid in prepared:
                raise TransferError('Товар добавлен дважды.')
            prepared[pid] = quantity
        fingerprint = json.dumps([source, destination, sorted(prepared.items()), str(comment or '')], ensure_ascii=False)
        with self._transaction(connection) as c:
            existing = c.execute('SELECT id,request_json FROM erp_stock_transfers WHERE idempotency_key=?', (key,)).fetchone()
            if existing:
                if existing['request_json'] != fingerprint:
                    raise TransferError('Ключ операции уже использован с другими данными.')
                return self._get(c, existing['id'])
            for wid in (source, destination):
                if not c.execute('SELECT 1 FROM erp_warehouses WHERE id=? AND active=1', (wid,)).fetchone():
                    raise TransferError('Склад не найден или отключён.')
            for pid in prepared:
                if not c.execute('SELECT 1 FROM catalog_excel_products WHERE id=? AND active=1 AND deleted_at IS NULL', (pid,)).fetchone():
                    raise TransferError('Товар не найден.')
                if c.execute('SELECT 1 FROM erp_product_bundles WHERE product_id=?', (pid,)).fetchone():
                    raise TransferError('Перемещайте физические компоненты комплекта.')
            c.execute("INSERT OR IGNORE INTO erp_document_sequences VALUES('transfer',0)")
            c.execute("UPDATE erp_document_sequences SET last_value=last_value+1 WHERE document_type='transfer'")
            number = 'ПМ-{:06d}'.format(c.execute("SELECT last_value FROM erp_document_sequences WHERE document_type='transfer'").fetchone()[0])
            identity = uuid.uuid4().hex
            c.execute('INSERT INTO erp_stock_transfers(id,number,from_warehouse_id,to_warehouse_id,status,comment,created_by,created_at,idempotency_key,request_json) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (identity, number, source, destination, 'draft', str(comment or ''), actor, now(), key, fingerprint))
            c.executemany('INSERT INTO erp_stock_transfer_items(transfer_id,product_id,quantity) VALUES(?,?,?)',
                          [(identity, pid, qty) for pid, qty in sorted(prepared.items())])
            return self._get(c, identity)

    def transition(self, identity, action, actor='', failure_hook=None, connection=None):
        transitions = {'send': ('draft', 'in_transit', 'sent_at'),
                       'receive': ('in_transit', 'received', 'received_at'),
                       'cancel': ('draft', 'cancelled', 'cancelled_at')}
        if action not in transitions:
            raise TransferError('Неизвестное действие.')
        before_status, after_status, date_column = transitions[action]
        with self._transaction(connection) as c:
            doc = self._get(c, identity)
            if doc['status'] == after_status:
                return doc
            if doc['status'] != before_status:
                raise TransferError('Действие недоступно в текущем статусе. После отправки нужна физическая приёмка; возврат оформляется отдельным перемещением.')
            stamp = now()
            if action != 'cancel':
                wid = doc['from_warehouse_id'] if action == 'send' else doc['to_warehouse_id']
                assert_products_unlocked(c, [i['product_id'] for i in doc['items']], TransferError)
                for item in doc['items']:
                    pid = item['product_id']
                    if action == 'send' and not c.execute('SELECT 1 FROM catalog_excel_products WHERE id=? AND active=1 AND deleted_at IS NULL', (pid,)).fetchone():
                        raise TransferError('Товар архивирован после создания черновика.')
                    delta = item['quantity'] * (-1 if action == 'send' else 1)
                    old = balance(c, pid, warehouse_id=wid)
                    if old + delta < 0:
                        raise TransferError('На складе-источнике недостаточно товара: {}'.format(item['name']))
                    write_balance(c, pid, old + delta, 'transfer', stamp, warehouse_id=wid)
                    c.execute(
                        'INSERT INTO catalog_stock_movements(id,product_id,movement_type,quantity_delta,stock_before,stock_after,idempotency_key,source_type,source_id,source_line_id,operation_kind,source_number,source,user_name,comment,created_at,warehouse_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex, pid, 'manual_adjustment', delta, old, old + delta,
                         'transfer:{}:{}:{}'.format(identity, action, pid), 'transfer', identity, str(item['id']),
                         action, doc['number'], 'Перемещение', actor,
                         '{} → {}; {}'.format(doc['from_name'], doc['to_name'], 'Отправка' if action == 'send' else 'Приёмка'), stamp, wid))
                    AuditJournal(self.database).record(
                        'product', pid, 'updated', item['name'], doc['number'],
                        before={'stock': old}, after={'stock': old + delta},
                        metadata={'transfer_id':identity, 'warehouse_id':wid, 'operation':action,
                                  'text_snapshot':'{}: {} → {}'.format(doc['number'], doc['from_name'], doc['to_name'])},
                        actor_id=actor, actor_name=actor, source='Перемещение', connection=c)
            c.execute('UPDATE erp_stock_transfers SET status=?, {}=? WHERE id=?'.format(date_column), (after_status, stamp, identity))
            if failure_hook:
                failure_hook(c)
            return self._get(c, identity)

    @staticmethod
    def _get(c, identity):
        row = c.execute('SELECT t.*,f.name AS from_name,d.name AS to_name FROM erp_stock_transfers t JOIN erp_warehouses f ON f.id=t.from_warehouse_id JOIN erp_warehouses d ON d.id=t.to_warehouse_id WHERE t.id=?', (identity,)).fetchone()
        if row is None:
            raise TransferError('Перемещение не найдено.')
        doc = dict(row)
        doc.pop('request_json')
        doc['items'] = [dict(i) for i in c.execute('SELECT i.*,p.excel_name_raw AS name FROM erp_stock_transfer_items i JOIN catalog_excel_products p ON p.id=i.product_id WHERE i.transfer_id=? ORDER BY i.id', (identity,))]
        return doc

    def get(self, identity):
        with self.database.connect() as c:
            return self._get(c, identity)

    def list(self):
        with self.database.connect() as c:
            return [self._get(c, r[0]) for r in c.execute('SELECT id FROM erp_stock_transfers ORDER BY created_at DESC LIMIT 200').fetchall()]


def distribution(connection, product_ids):
    """Sparse balances plus inbound transit; no assortment registration."""
    result = {}
    ids = list(dict.fromkeys(int(pid) for pid in product_ids))
    for offset in range(0, len(ids), 400):
        chunk = ids[offset:offset + 400]
        placeholders = ','.join('?' for _ in chunk)
        rows = connection.execute(
            'SELECT s.product_id,w.id,w.name,w.code,s.quantity,0 AS in_transit FROM erp_warehouse_stocks s JOIN erp_warehouses w ON w.id=s.warehouse_id WHERE s.product_id IN ({}) AND s.quantity>0 '
            'UNION ALL SELECT i.product_id,w.id,w.name,w.code,0,SUM(i.quantity) FROM erp_stock_transfer_items i JOIN erp_stock_transfers t ON t.id=i.transfer_id JOIN erp_warehouses w ON w.id=t.to_warehouse_id WHERE i.product_id IN ({}) AND t.status=\'in_transit\' GROUP BY i.product_id,w.id'.format(placeholders, placeholders), chunk + chunk)
        grouped = {}
        for row in rows:
            key = (row['product_id'], row['id'])
            if key in grouped:
                item = grouped[key]
                item['quantity'] += row['quantity']
                item['in_transit'] += row['in_transit']
            else:
                grouped[key] = dict(row)
        for (pid, unused), item in grouped.items():
            item['color'] = '#2563eb' if item['id'] == 'default' else {'HK': '#f28c28'}.get(item['code'], '#64748b')
            item['transit_only'] = not item['quantity'] and bool(item['in_transit'])
            item['label'] = '{}: {:g} шт.; в пути: {:g}'.format(item['name'], item['quantity'], item['in_transit'])
            result.setdefault(pid, []).append(item)
    return result


def product_stock_summary(connection, product_id):
    """Dense, read-only card view. Inbound transit is not available stock."""
    balances = {row['id']: row for row in distribution(connection, [product_id]).get(product_id, [])}
    component = connection.execute(
        'SELECT physical_stock FROM erp_component_inventory WHERE product_id=?',
        (product_id,),
    ).fetchone()
    bundle = connection.execute('SELECT 1 FROM erp_product_bundles WHERE product_id=?', (product_id,)).fetchone()
    warehouses = connection.execute(
        "SELECT id,name,code,active FROM erp_warehouses "
        "ORDER BY CASE WHEN id='default' THEN 0 ELSE 1 END,name,id"
    ).fetchall()
    result = []
    for warehouse in warehouses:
        wid = warehouse['id']
        stock = balances.get(wid, {})
        if not warehouse['active'] and not stock:
            continue
        confirmed = not (wid == 'default' and component is not None and component['physical_stock'] is None)
        result.append({
            'id': wid, 'name': warehouse['name'], 'code': warehouse['code'],
            'active': bool(warehouse['active']),
            'quantity': float(stock.get('quantity', 0)) if confirmed else None,
            'in_transit': float(stock.get('in_transit', 0)),
            'confirmed': confirmed,
            'editable': bool(warehouse['active']) and component is None and bundle is None,
        })
    return result
