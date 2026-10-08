"""Bounded CDEK followups. Every external action uses a fresh carrier response."""
import hashlib
import json
import time

from app.clients.cdek import CdekError
from app.services.cdek_delivery import normalize_delivery
from app.services.sms import normalize_phone

DAY = 86400
PICKUP = 'ACCEPTED_AT_PICK_UP_POINT'
FAILED_ATTEMPT = 'RETURNED_TO_RECIPIENT_CITY_WAREHOUSE'
INACTIVE = {'DELIVERED', 'POSTOMAT_RECEIVED', 'NOT_DELIVERED', 'REMOVED', 'INVALID'}


def trigger(data):
    if data.get('is_return') or data.get('status_code') in INACTIVE:
        return None
    kind = data.get('delivery_kind')
    if kind not in {'pvz', 'courier'}:
        return None
    if kind == 'pvz' and data.get('status_code') != PICKUP:
        return None
    code = PICKUP if kind == 'pvz' else FAILED_ATTEMPT
    dates = [e['epoch'] for e in data.get('events', []) if e['code'] == code]
    return (kind, min(dates)) if dates else None


class CdekFollowups:
    def __init__(self, delivery, sms, tasks, assignee, clock=time.time):
        self.delivery, self.sms, self.tasks = delivery, sms, tasks
        self.assignee, self.clock = assignee, clock

    def process(self, shipment):
        number = str(shipment.get('tracking') or '')
        entity = self.delivery.client.get_order(cdek_number=number)
        data = normalize_delivery(entity)
        if data.get('cdek_number') != number:
            raise CdekError('CDEK_MISMATCH', 'СДЭК вернул другую накладную.')
        if any(entity.get(k) for k in ('is_reverse', 'is_client_return')):
            return
        moment = trigger(data)
        if not moment:
            return
        kind, started = moment
        age = self.clock() - started
        if age < 5 * DAY:
            return
        if kind == 'pvz':
            point = self.delivery.client.get_delivery_point(entity.get('delivery_point'))
            if point.get('type') != 'PVZ':
                return
        if age >= 7 * DAY and self.tasks:
            number_label = ', '.join(shipment.get('orders') or []) or shipment.get('number') or 'без номера'
            title = 'СДЭК: позвонить покупателю — заказ {}, накладная {} (7 суток {})'.format(
                number_label, number, 'в ПВЗ' if kind == 'pvz' else 'после неудачной доставки')
            self.tasks.create_automated_micro(self.assignee, title, number, shipment['id'])
        # One action per waybill, even if repeated events change the history.
        if kind == 'pvz' and self.sms:
            key = 'cdek-pvz-sms-' + hashlib.sha256(number.encode()).hexdigest()[:32]
            if not self.sms.store.get(client_message_id=key):
                recipient = entity.get('recipient') or {}
                phones = {normalize_phone(p.get('number')) for p in recipient.get('phones', [])}
                if len(phones) != 1:
                    raise ValueError('Ambiguous CDEK recipient phone')
                order_ids = shipment.get('order_ids') or []
                if len(order_ids) != 1:
                    raise ValueError('Ambiguous ERP order link')
                if self.sms.store.has_waybill_notification(order_ids[0], next(iter(phones)), number):
                    return
                name = ' '.join(str(recipient.get('name') or '').split())
                if not name:
                    raise ValueError('Missing CDEK recipient name')
                text = '{}, Ваш заказ TicTacToy.ru ожидает получения ❤ Трекинг {} Отследить cdek.ru.'.format(name, number)
                self.sms.send(dict(client_message_id=key, phone=next(iter(phones)), text=text,
                    customer_name=name, order_id=order_ids[0], order_number=shipment.get('number') or ''),
                    {'id': 'system:cdek', 'name': 'Автоматически · СДЭК'})

    def run(self, shipments, limit=20):
        candidates = []
        for shipment in shipments:
            if not shipment.get('tracking'):
                continue
            moment = trigger(self.delivery.view(shipment))
            if moment and self.clock() - moment[1] >= 5 * DAY:
                candidates.append(shipment)
        candidates.sort(key=lambda s: s['id'])
        path = self.delivery.path / 'followups-cursor.json'
        try:
            cursor = json.loads(path.read_text(encoding='utf-8'))['after']
        except FileNotFoundError:
            cursor = ''
        candidates = [s for s in candidates if s['id'] > cursor] + [s for s in candidates if s['id'] <= cursor]
        result = {'checked': 0, 'errors': 0}
        deadline = time.monotonic() + 60
        for shipment in candidates[:limit]:
            if time.monotonic() >= deadline:
                break
            try:
                self.process(shipment)
                result['checked'] += 1
            except Exception:
                # No phone, recipient, credentials or provider response in logs.
                result['errors'] += 1
            finally:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'after': shipment['id']}), encoding='utf-8')
                temporary.replace(path)
        return result
