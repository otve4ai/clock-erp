import io
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from flask import Flask
from jinja2 import ChoiceLoader, DictLoader

from app.clients.cdek import CdekClient, CdekError
from app.cdek_payouts_routes import build_view, import_report, register_cdek_payouts_routes
from app.services.cdek_payouts import CdekPayouts, compact_order, clean_registries, reconcile
from app.services.cdek_sync import CdekSync


def entity(track='10300000001', **detail):
    return dict(cdek_number=track, number='123', recipient_currency='RUB',
                statuses=[dict(code='DELIVERED', name='Вручён', date_time='2026-10-01T12:00:00+0000'),
                          dict(code='CREATED', date_time='2026-09-01T12:00:00+0000')],
                packages=[{'items': [{'amount': 1, 'payment': {'value': 1000}}]}],
                delivery_detail=dict(date='2026-10-01', payment_sum=1000, total_sum=100, **detail))


def registry(track='10300000001', gross=1000, net=900, number='1', day='2026-10-02'):
    return dict(number=number, date=day, total=str(net), entries=[dict(track=track, gross=str(gross), net=str(net), basis='ORDER')])


class FinancialTest(unittest.TestCase):
    def test_pending_paid_and_partial_registry(self):
        o=compact_order(entity())
        rows,_=reconcile([o],[], '2026-09-01')
        self.assertEqual((rows[0]['category'], rows[0]['estimate']),('pending','900.00'))
        rows,_=reconcile([o],[registry(),registry()], '2026-09-01')
        self.assertEqual((rows[0]['category'],rows[0]['net']),('paid','900.00'))
        rows,_=reconcile([o],[registry(gross=600,net=500)],'2026-09-01')
        self.assertEqual((rows[0]['remaining'],rows[0]['estimate']),('400.00',None))

    def test_partial_buyout_uses_collected_not_item_cod(self):
        e=entity();e['delivery_detail']['payment_sum']=400
        row=reconcile([compact_order(e)],[registry(gross=400,net=300)],'2026-09-01')[0][0]
        self.assertEqual(row['category'],'paid')

    def test_return_refusal_zero_and_long_wait(self):
        e=entity();e['is_return']=True
        self.assertEqual(reconcile([compact_order(e)],[],'2026-09-01')[0][0]['category'],'other')
        e=entity();e['delivery_detail']={};e['statuses'][0]['code']='ACCEPTED_AT_PICK_UP_POINT'
        self.assertEqual(reconcile([compact_order(e)],[],'2026-09-01')[0][0]['category'],'expected')
        e['statuses'][0]['code']='NOT_DELIVERED'
        self.assertEqual(reconcile([compact_order(e)],[],'2026-09-01')[0][0]['category'],'other')

    def test_unknown_missing_payment_currency_or_history(self):
        e=entity();del e['delivery_detail']['payment_sum']
        self.assertEqual(reconcile([compact_order(e)],[],'2026-09-01')[0][0]['category'],'unknown')
        e=entity();e['recipient_currency']='USD'
        self.assertEqual(reconcile([compact_order(e)],[],'2026-09-01')[0][0]['category'],'unknown')
        self.assertEqual(reconcile([compact_order(entity())],[],'2026-10-02')[0][0]['category'],'unknown')

    def test_nonfinite_and_conflicting_registries_rejected(self):
        e=entity();e['delivery_detail']['payment_sum']='NaN'
        with self.assertRaises(CdekError): compact_order(e)
        with self.assertRaises(CdekError): reconcile([compact_order(entity())],[registry(),registry(net=899)],'2026-09-01')
        with self.assertRaises(CdekError): clean_registries([{}],'2026-10-01')

    def test_filters_apply_to_counts_money_and_split_payment_dates(self):
        row=compact_order(entity())
        rows,regs=reconcile([row],[registry(gross=400,net=350,day='2026-10-01'),registry(gross=600,net=550,number='2')],'2026-09-01')
        snap=dict(rows=rows,registries=regs,since='2026-09-01')
        result=build_view(snap,{'from':'2026-10-02','to':'2026-10-02','date_kind':'paid_date','mode':'paid'},date(2026,10,2))
        self.assertEqual(result['totals']['paid'],Decimal('550.00'))
        self.assertEqual(result['counts']['paid'],1)
        result=build_view(snap,{'from':'2026-10-01','to':'2026-10-02'},date(2026,10,2))
        self.assertEqual(result['counts']['all'],0)
        with self.assertRaises(ValueError):build_view(snap,{'from':'2026-10-03','to':'2026-10-02'},date(2026,10,2))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.client=mock.Mock(configured=True)
        self.client.get_registries.return_value=[]
        self.client.get_order.return_value=entity()
        self.service=CdekPayouts(self.temp.name,self.client,lambda:1790940000)

    def test_resumes_and_does_not_publish_incomplete_snapshot(self):
        result=self.service.sync_pending([{'cdek_number':'10300000001'}],limit=2)
        self.assertGreater(result['remaining'],0)
        self.assertEqual(self.service.snapshot(),{})
        result=self.service.sync_pending([{'cdek_number':'10300000001'}],limit=100)
        self.assertEqual(result['remaining'],0)
        self.assertEqual(self.service.snapshot()['rows'][0]['category'],'pending')
        self.assertEqual(self.client.get_registries.call_count,45)

    def test_failure_preserves_snapshot_and_records_visible_error(self):
        self.service.sync_pending([{'cdek_number':'10300000001'}])
        before=self.service.snapshot()
        self.client.get_registries.side_effect=CdekError('CDEK_NETWORK','Сеть недоступна')
        sync=CdekSync(self.service);sync.run(lambda:[])
        self.assertEqual(self.service.snapshot(),before)
        self.assertEqual(sync.summary()['outcome'],'error')

    def test_daily_job_resumes_stage_across_midnight(self):
        self.service.sync_pending([{'cdek_number':'10300000001'}],limit=2)
        original=self.service.read('stage.json',{})
        self.service.clock=lambda:1790940000+86400
        self.service.sync_pending([],limit=100)
        self.assertEqual(self.service.snapshot()['until'],original['until'])
        self.assertEqual(self.client.get_registries.call_count,45)
        self.assertEqual(len(self.service.snapshot()['rows']),1)

    def test_lost_order_retains_unknown_previous_row(self):
        self.service.sync_pending([{'cdek_number':'10300000001'}])
        self.client.get_order.side_effect=CdekError('CDEK_NOT_FOUND','Нет накладной')
        self.service.sync_pending([])
        self.assertEqual(self.service.snapshot()['rows'][0]['category'],'unknown')
        self.assertEqual(len(self.service.snapshot()['unresolved']),1)

    def test_corrupt_snapshot_is_not_replaced(self):
        p=Path(self.temp.name)/'snapshot.json';p.write_text('{bad')
        with self.assertRaises(CdekError):self.service.sync_pending([])
        self.assertEqual(p.read_text(),'{bad')

    def test_paid_and_pending_old_orders_are_revisited_without_ui_filters(self):
        self.service.sync_pending([{'cdek_number':'10300000001'}])
        self.client.get_order.reset_mock()
        self.service.sync_pending([])
        self.client.get_order.assert_called_once_with(cdek_number='10300000001')

    def test_routes_read_only_permissions_csrf_and_template(self):
        app=Flask(__name__,template_folder=str(Path(__file__).resolve().parents[1]/'app/templates'))
        app.jinja_loader=ChoiceLoader([DictLoader({'_sidebar.html':'','_favicon.html':''}),app.jinja_loader])
        app.jinja_env.globals.update(csrf_token=lambda:'test',static_asset_url=lambda f:'/static/'+f)
        app.add_url_rule('/app/analytics','analytics_page',lambda:'analytics')
        allowed=mock.Mock(return_value=True);csrf=mock.Mock()
        register_cdek_payouts_routes(app,self.service,lambda:[],allowed,csrf)
        web=app.test_client();response=web.get('/app/analytics/cdek-payouts')
        self.assertEqual(response.status_code,200)
        self.assertIn('Нет данных',response.get_data(as_text=True))
        self.client.get_order.assert_not_called();self.client.get_registries.assert_not_called()
        allowed.return_value=False
        self.assertEqual(web.get('/app/analytics/cdek-payouts/sync').status_code,403)
        allowed.return_value=True
        with mock.patch.object(CdekSync,'start') as start:
            self.assertEqual(web.post('/app/analytics/cdek-payouts/sync').status_code,202)
            csrf.assert_called_once();start.assert_called_once()
        self.service.sync_pending([{'cdek_number':'10300000001'}])
        for mode in ['pending','paid','expected','all','registries','unknown']:
            response=web.get('/app/analytics/cdek-payouts?mode='+mode)
            self.assertEqual(response.status_code,200)

    def test_import_only_tracks_and_start_no_private_data(self):
        import openpyxl
        book=openpyxl.Workbook();sheet=book.active
        sheet.append(['Номер заказа','Дата накладной','Телефон'])
        sheet.append([10300000001,'01.09.2026','secret-contact'])
        stream=io.BytesIO();book.save(stream)
        tracks,start=import_report(stream.getvalue())
        self.assertEqual((tracks,start),(['10300000001'],'2026-09-01'))
        with self.assertRaises(ValueError):import_report(b'not an xlsx')


class ClientTest(unittest.TestCase):
    def test_empty_registry_and_token_refresh(self):
        api=CdekClient(account='test',password='test')
        api._authorize=mock.Mock()
        api._request=mock.Mock(side_effect=[CdekError('CDEK_UNAUTHORIZED','expired'),{}])
        self.assertEqual(api.get_registries('2026-10-02'),[])
        self.assertEqual(api._authorize.call_count,2)
        api._request.return_value={'wrong':[]};api._request.side_effect=None
        with self.assertRaises(CdekError):api.get_registries('2026-10-02')


if __name__=='__main__':unittest.main()
