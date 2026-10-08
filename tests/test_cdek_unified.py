"""Unified CDEK workflow regressions; temporary stores, no live integrations."""
import json
import sqlite3
from pathlib import Path
import unittest
from unittest import mock

from app.clients.cdek import CdekError
from app.services.cdek_contacts import ContactJournals
from tests import test_cdek_sales as fixtures


class UnifiedServiceTest(unittest.TestCase):
    setUp = fixtures.SalesDeliveryTest.setUp
    seed = fixtures.SalesDeliveryTest.seed

    def test_one_save_one_event_and_retries_are_idempotent(self):
        group = self.seed(age=0)
        payload = dict(version=0, form_mode='unified', work='working', call_result='connected',
                       note='Обещал забрать завтра', next_action='Проверить получение', followup='2026-10-02')
        first = self.service.save_review(group, payload, 'Лера')
        second = self.service.save_review(group, payload, 'Лера')
        self.assertEqual(first, second)
        self.assertEqual(len(second['calls']), 1)
        row = self.service.rows(self.sales)[0]
        self.assertEqual(len(row['interactions']), 1)
        self.assertEqual(row['interactions'][0]['text'], payload['note'])
        self.assertIn('Звонок: связались', row['interactions'][0]['title'])
        self.assertIn('Проверить получение', row['interactions'][0]['title'])
        self.assertEqual(second['calls'][0]['at'], self.now)
        self.now += 60
        self.service.save_review(group, dict(version=1,form_mode='unified',work='working',note='Новая договорённость'), 'Лера')
        self.service.save_review(group, payload, 'Лера')
        saved = self.service.review(group['id'])
        self.assertEqual(saved['version'], 2)
        self.assertEqual(len(saved['calls']), 1)
        self.assertEqual([e['text'] for e in self.service.rows(self.sales)[0]['interactions']], ['Новая договорённость', payload['note']])

    def test_two_actions_in_same_second_keep_calls_with_their_own_comment(self):
        group = self.seed(age=0)
        for version, comment, result in ((0,'Первый звонок','no_answer'), (1,'Второй звонок','connected')):
            self.service.save_review(group,dict(version=version,form_mode='unified',work='working',note=comment,call_result=result),'Лера')
        events = self.service.rows(self.sales)[0]['interactions']
        self.assertEqual(events[0]['text'],'Второй звонок')
        self.assertIn('связались',events[0]['title'])
        self.assertEqual(events[1]['text'],'Первый звонок')
        self.assertNotIn('связались',events[1]['title'])

    def test_unreachable_requires_shared_comment_and_preserves_delivery(self):
        group = self.seed()
        before = self.delivery.view(group)
        for outcome in ('unreachable', 'other'):
            with self.assertRaises(CdekError) as raised:
                self.service.save_review(group, dict(version=0,form_mode='unified',work='closed',outcome=outcome,note=' '), 'Лера')
            self.assertEqual(raised.exception.field, 'note')
        saved = self.service.save_review(group, dict(version=0,form_mode='unified',work='closed',outcome='unreachable',note='Три звонка без ответа'), 'Лера')
        self.assertEqual(saved['calls'], [])
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row['issues'], [])
        self.assertEqual(row['interactions'][0]['text'], 'Три звонка без ответа')
        self.assertEqual(self.delivery.view(group), before)
        self.now += 61
        self.seed()  # Same milestone does not reopen.
        self.assertFalse(self.service.rows(self.sales)[0]['reopened'])
        self.now += 61
        self.seed('NOT_DELIVERED', age=0)
        self.assertTrue(self.service.rows(self.sales)[0]['reopened'])

    def test_no_answer_next_contact_and_note_without_call(self):
        group = self.seed(age=0)
        self.service.save_review(group, dict(version=0,form_mode='unified',work='working',call_result='no_answer',note='Перезвонить',next_action='Позвонить',followup='2026-10-02'), 'Лера')
        self.now += 86400
        row = self.service.rows(self.sales)[0]
        self.assertTrue(any('срок следующего' in x['reason'] for x in row['issues']))
        self.assertEqual(row['contacts']['call']['tone'], 'yellow')
        self.service.save_review(group, dict(version=1,form_mode='unified',work='new',note='Уточнить у СДЭК'), 'Лера')
        saved = self.service.review(group['id'])
        self.assertEqual(len(saved['calls']), 1)
        self.assertEqual(saved['followup'], '')
        self.assertEqual(saved['next_action'], '')

    def test_journal_history_includes_text_author_and_deduplicates_order_links(self):
        root = Path(self.temp.name)
        sms, mail = root / 'sms.db', root / 'mail.db'
        with sqlite3.connect(sms) as db:
            db.execute('CREATE TABLE sms_messages(id INTEGER,order_id TEXT,status TEXT,sent_at TEXT,updated_at TEXT,created_at TEXT,repair_id TEXT,message_text TEXT,sent_by_name TEXT,created_by_name TEXT)')
            db.execute("INSERT INTO sms_messages VALUES(1,'42','delivered','2026-10-01T07:00:00Z','','2026-10-01T06:59:00Z',NULL,'Текст SMS','Лера','Лера')")
        with sqlite3.connect(mail) as db:
            db.execute('CREATE TABLE mail_outbox(id INTEGER,thread_id INTEGER,state TEXT,sent_at TEXT,updated_at TEXT,created_at TEXT,author_id INTEGER,idempotency_key TEXT)')
            db.execute('CREATE TABLE mail_links(thread_id INTEGER,entity_type TEXT,entity_id TEXT)')
            db.executemany("INSERT INTO mail_links VALUES(1,'order',?)", [('42',),('43',)])
            db.execute("INSERT INTO mail_outbox VALUES(1,1,'sent','2026-10-01T07:01:00Z','','2026-10-01T06:59:00Z',0,'cdek-pvz-v1:test')")
        records = ContactJournals(sms,mail)([dict(id='s',order_ids=['42','43'])])['s']
        self.assertEqual(len(records['events']),2)
        self.assertEqual(records['sms']['text'],'Текст SMS')
        self.assertEqual(records['events'][0]['actor'],'Лера')
        self.assertEqual(records['events'][1]['actor'],'Автоматически')
        self.assertEqual(records['email']['url'],'/app/mail?thread=1')


class UnifiedRoutesTest(unittest.TestCase):
    setUp = fixtures.CdekSalesRoutesTest.setUp
    seed = fixtures.CdekSalesRoutesTest.seed

    def test_validation_and_storage_errors_keep_entire_form(self):
        group = self.seed()
        url = '/sales/cdek/'+group['id']+'/review'
        payload = dict(version=0,form_mode='unified',review_work='closed',outcome='unreachable',call_result='no_answer',note='')
        response = self.client.post(url,data=payload)
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code,400)
        self.assertIn('id="error-note"',html)
        self.assertIn('value="no_answer" checked',html)
        self.assertRegex(html, r'value="unreachable"\s+selected')
        self.assertEqual(self.service.review(group['id'])['version'],0)
        payload.update(review_work='working', note='<Новая заметка>', next_action='Повторный звонок',followup='2026-10-02')
        with mock.patch('app.services.cdek_sales.os.replace',side_effect=OSError('disk')):
            response=self.client.post(url,data=payload)
        html=response.get_data(as_text=True)
        self.assertEqual(response.status_code,400)
        self.assertIn('&lt;Новая заметка&gt;',html)
        self.assertNotIn('name="next_action"',html)
        self.assertNotIn('name="followup"',html)
        self.assertEqual(self.service.review(group['id'])['version'],0)
        response=self.client.post(url,data=payload,follow_redirects=True)
        self.assertEqual(response.status_code,200)
        self.assertIn('value="" checked', response.get_data(as_text=True))
        self.client.post(url,data=payload)
        self.assertEqual(len(self.service.review(group['id'])['calls']),1)

    def test_conflict_does_not_overwrite_or_discard_new_input(self):
        group=self.seed()
        self.service.save_review(group,dict(version=0,work='working',note='Другой сотрудник'),'Анна')
        payload=dict(version=0,form_mode='unified',review_work='working',call_result='connected',note='Несохранённый текст',next_action='Проверить',followup='2026-10-02')
        response=self.client.post('/sales/cdek/'+group['id']+'/review',data=payload)
        self.assertEqual(response.status_code,409)
        self.assertIn('Несохранённый текст',response.get_data(as_text=True))
        self.assertIn('value="connected" checked',response.get_data(as_text=True))
        self.assertEqual(self.service.review(group['id'])['note'],'Другой сотрудник')

    def test_saved_only_history_and_safe_read_links(self):
        group=self.seed(age=0)
        self.service.contacts=lambda groups:{g['id']:{'email':{'status':'sent','at':self.now,'url':'/app/mail?thread=12'}, 'sms':{'status':'sent','at':self.now,'text':'<script>unsafe</script>'}} for g in groups}
        before=self.api.get_order.call_count
        html=self.client.get('/sales/cdek?mode=all&shipment='+group['id']).get_data(as_text=True)
        self.assertIn('/app/mail?thread=12',html)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;',html)
        self.assertNotIn('Зафиксировать звонок',html)
        self.assertNotIn('История звонков',html)
        self.assertIn('value="" checked',html)
        self.assertIn('Действий пока нет',html)
        self.assertEqual(self.api.get_order.call_count,before)


if __name__ == '__main__':
    unittest.main()
