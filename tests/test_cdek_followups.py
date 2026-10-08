import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.services.cdek_delivery import normalize_delivery
from app.services.cdek_followups import CdekFollowups, DAY, trigger
from app.services.sms import SmsStore, SmsService
from app.tasks.migrations import migrate_database
from app.tasks.repository import TasksRepository
from app.tasks.services import TasksService
from scripts.cdek_followup_worker import assignee


class FollowupsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.entity = dict(cdek_number='1234567890', tariff_code=136, delivery_point='TEST',
            recipient={'name':'Клиент', 'phones':[{'number':'+79991234567'}]},
            statuses=[{'code':'ACCEPTED_AT_PICK_UP_POINT', 'date_time':'2026-10-01T12:00:00+03:00'}])
        self.start = normalize_delivery(self.entity)['events'][0]['epoch']
        self.now = self.start + 5*DAY
        self.delivery = Mock(path=root)
        self.delivery.client.get_order.side_effect = lambda **kw: copy.deepcopy(self.entity)
        self.delivery.client.get_delivery_point.return_value = {'type':'PVZ'}
        self.delivery.view.side_effect = lambda shipment: normalize_delivery(self.entity)
        self.store = SmsStore(root/'sms.db')
        self.store.initialize()
        self.provider = Mock()
        self.provider.send.return_value = {'status':'ok','messages':[{'status':'accepted','smscId':'1'}]}
        path = root/'tasks-module.db'
        migrate_database(path)
        self.person = {'id':2,'active':1}
        self.repo = TasksRepository(path)
        self.tasks = TasksService(self.repo, lambda uid:self.person if uid==2 else None)
        self.worker = CdekFollowups(self.delivery, SmsService(self.store,self.provider), self.tasks,
                                   self.person, clock=lambda:self.now)
        self.shipment = {'id':'a'*64,'tracking':'1234567890','orders':['123'],'order_ids':['123']}

    def count_tasks(self):
        with self.repo.transaction() as session:
            return session.connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]

    def test_sms_at_five_days_once_and_call_at_seven_days_once(self):
        self.now -= 1
        self.worker.process(self.shipment)
        self.provider.send.assert_not_called()
        self.now += 1
        self.worker.process(self.shipment)
        self.worker.process(self.shipment)
        self.assertEqual(self.provider.send.call_count,1)
        self.assertEqual(self.count_tasks(),0)
        self.now = self.start + 7*DAY
        self.worker.process(self.shipment)
        self.worker.process(self.shipment)
        self.assertEqual(self.count_tasks(),1)
        with self.repo.transaction() as session:
            task=session.get(1)
        self.assertEqual(task['assigned_to'],2)
        self.assertEqual(task['related_entity_label'],'a'*64)
        self.assertEqual(task['task_type'],'micro')
        self.tasks.mutate(self.person,task['id'],{'version':task['version'],'status':'done'},'status')
        self.worker.process(self.shipment)
        self.assertEqual(self.count_tasks(),1)

    def test_courier_first_failure_not_arrival_or_dispatch(self):
        self.entity.update(tariff_code=137,delivery_point=None)
        for code in ('ACCEPTED_IN_RECIPIENT_CITY','TAKEN_BY_COURIER'):
            self.entity['statuses'][0]['code']=code
            self.now=self.start+20*DAY
            self.worker.process(self.shipment)
            self.assertEqual(self.count_tasks(),0)
        self.entity['statuses'][0]['code']='RETURNED_TO_RECIPIENT_CITY_WAREHOUSE'
        self.entity['statuses'].append({'code':'TAKEN_BY_COURIER','date_time':'2026-10-07T12:00:00+03:00'})
        self.now=self.start+7*DAY-1
        self.worker.process(self.shipment)
        self.assertEqual(self.count_tasks(),0)
        self.now+=1
        self.worker.process(self.shipment)
        self.assertEqual(self.count_tasks(),1)
        self.provider.send.assert_not_called()

    def test_fresh_received_return_cancelled_unknown_prevent_every_action(self):
        self.now=self.start+10*DAY
        original=copy.deepcopy(self.entity)
        for code in ('DELIVERED','NOT_DELIVERED','REMOVED','POSTOMAT_RECEIVED','INVALID'):
            self.entity=copy.deepcopy(original)
            self.entity['statuses'].append({'code':code,'date_time':'2026-10-06T12:00:00+03:00'})
            self.worker.process(self.shipment)
        for field in ('is_return','is_reverse','is_client_return'):
            self.entity=copy.deepcopy(original)
            self.entity[field]=True
            self.worker.process(self.shipment)
        self.entity=copy.deepcopy(original)
        self.entity['tariff_code']=999
        self.worker.process(self.shipment)
        self.provider.send.assert_not_called()
        self.assertEqual(self.count_tasks(),0)

    def test_bad_phone_does_not_prevent_call_task(self):
        self.now=self.start+7*DAY
        self.entity['recipient']['phones']=[]
        with self.assertRaises(ValueError):
            self.worker.process(self.shipment)
        self.assertEqual(self.count_tasks(),1)

    def test_repeated_pickup_keeps_first_time(self):
        self.entity['statuses'].append({'code':'ACCEPTED_AT_PICK_UP_POINT','date_time':'2026-10-05T12:00:00+03:00'})
        self.assertEqual(trigger(normalize_delivery(self.entity))[1],self.start)

    def test_parallel_task_creation_and_deleted_task_do_not_duplicate(self):
        from concurrent.futures import ThreadPoolExecutor
        def create():
            return self.tasks.create_automated_micro(self.person,'Позвонить','1234567890','a'*64)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks=list(pool.map(lambda _:create(),range(2)))
        self.assertEqual(tasks[0]['id'],tasks[1]['id'])
        task=tasks[0]
        self.tasks.mutate(self.person,task['id'],{'version':task['version']},'delete')
        self.assertEqual(create()['id'],task['id'])
        self.assertEqual(self.count_tasks(),1)

    def test_unknown_provider_response_is_not_retried(self):
        from app.clients.smsbliss import SmsBlissUnknownDelivery
        self.provider.send.side_effect=SmsBlissUnknownDelivery('uncertain')
        self.worker.process(self.shipment)
        self.worker.process(self.shipment)
        self.assertEqual(self.provider.send.call_count,1)

    def test_stale_cache_cannot_send_after_receipt(self):
        cached=normalize_delivery(self.entity)
        self.delivery.view.side_effect=lambda shipment:cached
        self.entity['statuses'].append({'code':'DELIVERED','date_time':'2026-10-05T12:00:00+03:00'})
        result=self.worker.run([self.shipment])
        self.assertEqual(result['errors'],0)
        self.provider.send.assert_not_called()
        self.assertEqual(self.count_tasks(),0)

    def test_identity_requires_both_login_and_email(self):
        import sqlite3
        path=Path(self.temp.name)/'auth.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE users(id INTEGER, login TEXT,email TEXT,active INTEGER)')
            db.execute("INSERT INTO users VALUES(2,'ops','lera@mail.ru',1)")
        self.assertEqual(assignee(path),self.person)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE users SET email='someone@example.test'")
        with self.assertRaises(ValueError):
            assignee(path)
