"""Isolated local preview: no production data, integrations or real recipients."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime, timedelta
from app.services.cdek_delivery import MOSCOW
from app.services.cdek_sales import group_sales
from tests.test_cdek_sales import CdekSalesRoutesTest, sale

fixture = CdekSalesRoutesTest()
fixture.setUp()
fixture.now = datetime(2026, 10, 8, 12, tzinfo=MOSCOW).timestamp()
fixture.app.static_folder = str(Path(__file__).resolve().parents[1] / 'app/static')
fixture.sales = []
examples = [('10428','1080000001','ACCEPTED_AT_PICK_UP_POINT',8,False),('10431','1080000002','NOT_DELIVERED',1,False),('10439','1080000003','DELIVERED',0,True),('10445','1080000004','SENT_TO_RECIPIENT_CITY',1,False),('10450','1080000005','DELIVERED',0,False)]
for number, track, code, age, returned in examples:
    item = sale(number, track, product_name='Часы ZIIRO Eclipse · Black', created_at='2026-09-28T10:00:00+03:00')
    fixture.sales.append(item)
    fixture.seed(code, age, returned, sales=[item])
first, refused, returned = [group_sales([s])[0] for s in fixture.sales[:3]]
fixture.service.save_review(first, dict(version=0, form_mode='unified', work='working', note='Клиент планирует забрать заказ завтра.', next_action='Проверить, забрал ли клиент заказ', followup='2026-10-09',call_result='connected',call_at='2026-10-07T11:20',call_note='Договорились о получении завтра'), 'Анна Смирнова')
fixture.service.save_review(refused, dict(version=0, form_mode='unified', work='closed',outcome='unreachable',note='Три звонка без ответа. Email и SMS отправлены, реакции нет.',call_result='no_answer'), 'Иван Петров')
fixture.service.save_review(returned, dict(version=0, work='new',note='Проверить комплектность возврата.'), 'Анна Смирнова')
# Multiple saved actions, using only the temporary fixture store.
for i in range(3):
    fixture.now += 61
    previous = fixture.service.review(first['id'])
    fixture.service.save_review(first, dict(version=previous['version'], form_mode='unified',work='working', note='Повторный контакт: договорились проверить получение.', next_action='Проверить получение', followup='2026-10-09',call_result='no_answer' if i < 2 else 'connected'), 'Лера')
def contacts(groups):
    return {g['id']: {
        'email': {'status':'sent','at':fixture.now-3600,'url':'/app/mail?thread=1'},
        'sms':{'status':'failed' if g['id']==returned['id'] else 'sent','at':fixture.now-1800,'text':'Тестовое сообщение: ваш заказ ожидает получения.'},
        'events': [dict(channel='email',at=fixture.now-3600,actor='Автоматически',title='Email отправлен',text='',url='/app/mail?thread=1'),dict(channel='sms',at=fixture.now-1800,actor='Лера',title='SMS отправлено',text='Тестовое сообщение: ваш заказ ожидает получения.',url='')]
    } for i,g in enumerate(groups) if i<3}
fixture.service.contacts=contacts
# Multi-event fixture uses the same carrier normalizer as production.
entity = dict(tariff_code=136, delivery_point='TEST1', cdek_number=first['tracking'], is_return=False, statuses=[dict(code='ACCEPTED_AT_PICK_UP_POINT',name='Готов к выдаче',city='Москва',date_time='2026-09-29T12:00:00+03:00')]+[dict(code='SENT_TO_RECIPIENT_CITY',name='В пути',city='Москва' if i%2 else 'Санкт-Петербург',description='Отправление передано в следующий пункт',date_time=(datetime(2026,9,29,10,tzinfo=MOSCOW)-timedelta(hours=i*6)).isoformat()) for i in range(6)])
fixture.now += 61
fixture.api.get_order.return_value=entity
fixture.delivery.sync(first)
print('CLOSED /sales/cdek?mode=all&shipment='+refused['id'], flush=True)
print('PREVIEW /sales/cdek?mode=all&shipment='+first['id'], flush=True)
fixture.app.run(host='127.0.0.1',port=5064,debug=False,use_reloader=False)
