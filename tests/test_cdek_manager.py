"""Workflow regressions use temporary stores and mocked external clients only."""
import json
import sqlite3
import unittest
from pathlib import Path

from app.clients.cdek import CdekError
from app.services.cdek_contacts import ContactJournals, contact_states
from tests import test_cdek_sales as fixtures
sale = fixtures.sale


class ManagerWorkflowTest(unittest.TestCase):
    setUp = fixtures.SalesDeliveryTest.setUp
    seed = fixtures.SalesDeliveryTest.seed
    def test_refusal_closes_without_changing_carrier_and_new_return_reopens_once(self):
        group = self.seed('NOT_DELIVERED', age=0)
        self.service.save_review(group, dict(version=0, work='closed', outcome='refused', note='Отказ'), 'Анна')
        row = self.service.rows(self.sales)[0]
        self.assertEqual((row['label'], row['work'], row['issues']), ('Не вручён', 'closed', []))
        self.assertEqual(self.service.summary([row])[1]['problems'], 0)
        self.now += 61
        self.seed('NOT_DELIVERED', age=0)  # Repeat with a new timestamp is still the same situation.
        self.assertEqual(self.service.rows(self.sales)[0]['work'], 'closed')
        self.now += 61
        self.seed('DELIVERED', age=0, is_return=True)
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row['work'], 'new')
        self.assertEqual(row['review']['outcome'], 'refused')
        self.service.save_review(group, dict(version=1, work='closed', outcome='return_checked', note='Проверен'), 'Анна')
        self.now += 61
        self.seed('DELIVERED', age=0, is_return=True)
        self.assertEqual(self.service.rows(self.sales)[0]['work'], 'closed')
        self.assertEqual([h['outcome'] for h in self.service.review(group['id'])['history']], ['refused', 'return_checked'])

    def test_due_followup_rescheduling_and_preserving_confirmed_dates(self):
        group = self.seed(age=0)
        self.service.save_review(group, dict(version=0, work='working', followup='2026-10-02', next_action='Проверить получение', storage_until='2026-10-10'), 'Анна')
        self.assertEqual(self.service.rows(self.sales)[0]['work'], 'working')

        self.now += 86400
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row['work'], 'new')
        self.assertTrue(any(i['category'] == 'followup' for i in row['issues']))
        self.service.save_review(group, dict(version=1, work='working', followup='2026-10-05', next_action='Перезвонить'), 'Иван')
        saved = self.service.review(group['id'])
        self.assertEqual(saved['storage_until'], '2026-10-10')
        self.assertEqual(saved['history'][0]['followup'], '2026-10-02')
        self.assertEqual(self.service.rows(self.sales)[0]['work'], 'working')

    def test_due_reminder_survives_saving_only_a_note(self):
        group = self.seed('DELIVERED', age=0)
        self.service.save_review(group, dict(version=0, work='working', followup='2026-10-01', next_action='Уточнить'), 'A')
        self.service.save_review(group, dict(version=1, work='new', note='Ещё не связались'), 'A')
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row['work'], 'new')
        self.assertTrue(row['issues'])

    def test_completion_requires_outcome_and_return_received_and_other_explanation(self):
        group = self.seed('NOT_DELIVERED')
        for extra in ({}, {'outcome': 'return_checked'}, {'outcome': 'other'}, {'outcome': 'invented'}):
            with self.assertRaises(CdekError):
                self.service.save_review(group, dict(version=0, work='closed', **extra), 'A')
        self.service.save_review(group, dict(version=0, work='closed', outcome='other', outcome_note='Решено', note='История'), 'A')
        self.assertFalse(self.service.rows(self.sales)[0]['issues'])

    def test_calls_append_and_latest_actual_call_wins_without_network(self):
        group = self.seed('NOT_DELIVERED', age=0)
        before = self.api.get_order.call_count
        for version, result, at in [(0, 'no_answer', '2026-10-01T09:00'), (1, 'connected', '2026-10-01T09:30'), (2, 'no_answer', '2026-09-30T09:00')]:
            self.service.save_review(group, dict(version=version, work='working', call_result=result, call_at=at, call_note='Позвонить завтра'), 'Анна')
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row['contacts']['call']['state'], 'connected')
        self.assertIn('Анна', row['contacts']['call']['tooltip'])
        self.assertEqual(len(row['review']['calls']), 3)
        self.assertEqual(self.api.get_order.call_count, before)
        with self.assertRaises(CdekError):
            self.service.save_review(group, dict(version=3, work='working', call_result='connected', call_at='2027-01-01T09:00'), 'Анна')

    def test_legacy_return_migrates_lazily_without_losing_original_history(self):
        group = self.seed('DELIVERED', age=0, is_return=True)
        self.service.review_path.mkdir()
        path = self.service.review_path / (group['id'] + '.json')
        old = dict(version=5, work='closed', note='Старый комментарий', closed_event=self.delivery.view(group)['date_display'])
        path.write_text(json.dumps(old), encoding='utf-8')
        self.assertEqual(self.service.rows(self.sales)[0]['review']['outcome'], 'return_checked')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), old)
        self.service.save_review(group, dict(version=5, work='working', note=old['note']), 'Анна')
        self.assertEqual(self.service.review(group['id'])['history'][0]['note'], old['note'])

    def test_real_journals_exact_order_identity_and_provider_results(self):
        root = Path(self.temp.name)
        sms, mail = root/'sms.db', root/'mail.db'
        with sqlite3.connect(sms) as db:
            db.execute('CREATE TABLE sms_messages(id INTEGER, order_id TEXT, status TEXT, sent_at TEXT, updated_at TEXT, created_at TEXT, repair_id TEXT)')
            db.execute("INSERT INTO sms_messages VALUES(1,'42','delivered','2026-10-01T07:00:00Z','2026-10-01T07:01:00Z','2026-10-01T06:59:00Z',NULL)")
        with sqlite3.connect(mail) as db:
            db.execute('CREATE TABLE mail_outbox(id INTEGER, thread_id INTEGER, state TEXT, sent_at TEXT, updated_at TEXT, created_at TEXT)')
            db.execute('CREATE TABLE mail_links(thread_id INTEGER,entity_type TEXT,entity_id TEXT)')
            db.execute("INSERT INTO mail_links VALUES(1,'order','42')")
            db.execute("INSERT INTO mail_outbox VALUES(1,1,'failed',NULL,'2026-10-01T07:00:00Z','2026-10-01T06:59:00Z')")
        self.sales = [sale(external_order_id='42')]
        self.seed()
        self.service.contacts = ContactJournals(sms, mail)
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row['contacts']['sms']['label'], 'Отправлено')
        self.assertEqual(row['contacts']['email']['state'], 'error')
        with sqlite3.connect(mail) as db:
            db.execute("UPDATE mail_outbox SET state='sent', sent_at='2026-10-01T07:05:00Z'")
        self.assertEqual(self.service.rows(self.sales)[0]['contacts']['email']['state'], 'sent')
        self.sales.append(sale(track='9876543210', external_order_id='42'))
        self.assertTrue(all(r['contacts']['sms']['state'] == 'unknown' for r in self.service.rows(self.sales)))

    def test_all_contact_states_unknown_and_not_applicable_are_distinct(self):
        for raw, expected in [('created','pending'),('failed','error'),('accepted','sent'),('queued_sms','sent'),('unknown','unknown')]:
            value = contact_states({'sms': {'status':raw}}, {})['sms']
            self.assertEqual(value['state'], expected)
        unknown = contact_states({}, {})['email']
        na = contact_states({}, {}, True)['email']
        self.assertNotEqual(unknown['mark'], na['mark'])
        self.assertEqual(unknown['label'], 'Нет данных')


class ManagerRoutesTest(unittest.TestCase):
    setUp = fixtures.CdekSalesRoutesTest.setUp
    seed = fixtures.CdekSalesRoutesTest.seed
    def test_closed_filter_and_selected_card_survive_exclusion(self):
        group = self.seed('NOT_DELIVERED', age=0)
        before = self.api.get_order.call_count
        response = self.client.post('/sales/cdek/'+group['id']+'/review', data=dict(version=0, review_work='closed', outcome='refused', note='Отказ'), follow_redirects=True)
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Найдено 0', html)
        self.assertIn('id="cdek-manager-form"', html)
        self.assertIn('Клиент отказался', html)
        self.assertIn('Найдено 1', self.client.get('/sales/cdek?work=closed').get_data(as_text=True))
        self.assertEqual(self.api.get_order.call_count, before)

    def test_history_before_manager_contacts_and_cache_poll_is_read_only(self):
        group = self.seed()
        before = self.api.get_order.call_count
        html = self.client.get('/sales/cdek?shipment='+group['id']).get_data(as_text=True)
        self.assertLess(html.index('aria-label="История доставки"'), html.index('id="cdek-manager-form"'))
        self.assertNotIn('Сроки хранения и доставки', html)
        self.assertIn('<th>Контакт с клиентом</th><th>Обработка</th>', html)
        response = self.client.get('/sales/cdek/contacts?id='+group['id'])
        self.assertEqual(response.json[group['id']]['email']['state'], 'unknown')
        self.assertEqual(self.api.get_order.call_count, before)
        self.allowed.return_value = False
        self.assertEqual(self.client.get('/sales/cdek/contacts').status_code, 403)
