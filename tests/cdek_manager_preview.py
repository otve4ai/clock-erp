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
fixture.now = datetime(2026, 10, 7, 12, tzinfo=MOSCOW).timestamp()
fixture.app.static_folder = str(Path(__file__).resolve().parents[1] / 'app/static')
fixture.sales = []
examples = [('10428','1080000001','ACCEPTED_AT_PICK_UP_POINT',8,False),('10431','1080000002','NOT_DELIVERED',1,False),('10439','1080000003','DELIVERED',0,True),('10445','1080000004','SENT_TO_RECIPIENT_CITY',1,False),('10450','1080000005','DELIVERED',0,False)]
for number, track, code, age, returned in examples:
    item = sale(number, track, product_name='Часы ZIIRO Eclipse · Black', created_at='2026-09-28T10:00:00+03:00')
    fixture.sales.append(item)
    fixture.seed(code, age, returned, sales=[item])
first, refused, returned = [group_sales([s])[0] for s in fixture.sales[:3]]
fixture.service.save_review(first, dict(version=0, work='working', note='Клиент планирует забрать заказ завтра.', next_action='Проверить, забрал ли клиент заказ', followup='2026-10-08',call_result='connected',call_at='2026-10-07T11:20',call_note='Договорились о получении завтра'), 'Анна Смирнова')
fixture.service.save_review(refused, dict(version=0, work='closed',outcome='refused',note='Клиент отказался: не подошла модель.'), 'Иван Петров')
fixture.service.save_review(returned, dict(version=0, work='new',note='Проверить комплектность возврата.'), 'Анна Смирнова')
def contacts(groups):
    return {g['id']: {'email': {'status':'sent','at':fixture.now-3600}, 'sms':{'status':'failed' if i==2 else 'delivered','at':fixture.now-1800}} for i,g in enumerate(groups) if i<3}
fixture.service.contacts=contacts
# Multi-event fixture uses the same carrier normalizer as production.
entity = dict(cdek_number=first['tracking'], is_return=False, statuses=[dict(code='ACCEPTED_AT_PICK_UP_POINT',name='Готов к выдаче',city='Москва',date_time='2026-09-29T12:00:00+03:00')]+[dict(code='SENT_TO_RECIPIENT_CITY',name='В пути',city='Москва' if i%2 else 'Санкт-Петербург',description='Отправление передано в следующий пункт',date_time=(datetime(2026,9,29,10,tzinfo=MOSCOW)-timedelta(hours=i*6)).isoformat()) for i in range(6)])
fixture.now += 61
fixture.api.get_order.return_value=entity
fixture.delivery.sync(first)
@fixture.app.before_request
def no_external():
    from flask import request, abort
    if request.method!='GET':
        abort(403)
print('PREVIEW /sales/cdek?mode=all&shipment='+first['id'], flush=True)
fixture.app.run(host='127.0.0.1',port=5063,debug=False,use_reloader=False)
