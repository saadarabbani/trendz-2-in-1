"""End-to-end sync: the 'default' test database plays a shop laptop, the 'hub' test database plays the cloud.

Business code always uses the default connection, so `on_hub()` briefly points it at the hub database to do cloud-side
work (setting up shops, the owner locking a month, an online order reserving a phone)."""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
import json,uuid
from django.apps import apps
from django.urls import reverse
from django.db import DatabaseError,connections
from django.test import TestCase,TransactionTestCase
from django.utils import timezone
from shop.models import *
from shop.tests import imei
from shop.tests_transfers import transfer_setup
from shop import accessories,catalog_entry,coa,easy_retail,money,returns,shop_accounts,shopdefaults,stock_adjust,services as s
from . import agent,engine,hub_views,registry,triggers
from .models import Applied,Change,Conflict,Node,NodeState

D=Decimal


@contextmanager
def on_hub():
    conn=connections['default'];saved=conn.settings_dict['NAME']
    conn.close();conn.settings_dict['NAME']=connections['hub'].settings_dict['NAME']
    try:yield
    finally:conn.close();conn.settings_dict['NAME']=saved


class InProcessHub:
    """The hub's HTTP API, called directly against the hub test database."""
    def __init__(self):self.fail_after_push=False;self.offline=False
    def __call__(self,path,query,payload,key):
        if self.offline:raise agent.HubUnavailable('No connection to the cloud.')
        node=Node.objects.using('hub').select_related('branch').get(key_hash=hub_views.key_hash(key))
        query=query or {}
        if path=='/api/sync/hello/':return hub_views.do_hello(node,'hub')
        if path=='/api/sync/snapshot/':return hub_views.do_snapshot(node,query['table'],query.get('after'),'hub')
        if path=='/api/sync/push/':
            answer=hub_views.do_push(node,payload,'hub')
            if self.fail_after_push:raise agent.HubUnavailable('Connection dropped before the answer arrived.')
            return answer
        if path=='/api/sync/pull/':return hub_views.do_pull(node,int(query['since']),'hub',pending=query.get('pending'))
        raise AssertionError(path)


class Registry(TestCase):
    def test_every_model_is_classified_exactly_once(self):
        listed=registry.PUSH+registry.CLOUD+registry.LOCAL
        self.assertEqual(len(listed),len(set(listed)))
        actual={m._meta.label for m in apps.get_app_config('shop').get_models()}|{'auth.User'}
        self.assertEqual(actual-set(listed),set(),'classify new models in sync/registry.py')


class Offline(TransactionTestCase):
    databases={'default','hub'}
    serialized_rollback=True

    @classmethod
    def _fixture_setup(cls):
        # Other test classes may leave rows behind (a flush re-creates content types), so start both databases empty
        # before the migrations' data is restored.
        from django.core.management import call_command
        for alias in cls.databases:call_command('flush',database=alias,interactive=False,inhibit_post_migrate=True,verbosity=0)
        super()._fixture_setup()

    def setUp(self):
        with on_hub():
            transfer_setup(self);coa.install_standard(self.user);self.today=timezone.localdate();self.n=0
            self.login=shop_accounts.create_login(self.branch,'laptop-a','Strong-pass-for-LA-77!')
        self.key='test-key-'+'x'*30
        self.node=Node.objects.using('hub').create(node_no=1,branch_id=self.branch.pk,name='Counter laptop',key_hash=hub_views.key_hash(self.key))
        triggers.install('node','default');self.addCleanup(triggers.install,'hub','default')
        self.wire=InProcessHub();self.hub=agent.Hub(url='http://hub',key=self.key,transport=self.wire)
        agent.provision(self.hub)

    # ---- shop work on the laptop
    def buy(self,number=None,cost=50000):
        self.n+=1
        data=dict(branch=self.branch,party_name='Laptop Supplier',product=self.product,quantity=1,price=cost,asking_price=0,pta_status='PTA',device_type='Used',
                  condition='Good',warehouse=self.warehouse,staff=self.buyer,handler=self.handler,posting_date=self.today,payment_status='full',method='Cash',
                  imeis=[[imei(number or 700+self.n)]])
        return easy_retail.purchase(data,self.user)
    def sell(self,device,price=60000,day=None):
        data=dict(branch=self.branch,party_name='Walk-in Laptop',phone=device,price=price,pta_status='PTA',staff=self.seller,handler=self.handler,
                  posting_date=day or self.today,payment_status='full',method='Cash')
        return easy_retail.sale(data,self.user)
    def device_of(self,bill):return bill.reference.lines.get().device
    def sync(self):return agent.cycle(self.hub)
    def hub_totals(self,doc):
        rows=LedgerEntry.objects.using('hub').filter(document_id=doc.pk)
        return sum(r.debit for r in rows),sum(r.credit for r in rows),rows.count()

    # ---- tests
    def test_the_laptop_starts_as_a_copy_of_the_cloud_with_its_own_id_range(self):
        state=NodeState.objects.get()
        self.assertEqual((state.node_no,state.branch_id),(1,self.branch.pk))
        self.assertEqual(Account.objects.count(),Account.objects.using('hub').count())
        self.assertTrue(Branch.objects.filter(pk=self.branch.pk).exists())
        bill=self.buy()
        self.assertGreaterEqual(LedgerEntry.objects.filter(document=bill).order_by('pk').first().pk,agent.ID_RANGE)

    def test_purchases_and_sales_made_offline_reach_the_cloud_complete(self):
        self.wire.offline=True
        bill=self.buy();sale=self.sell(self.device_of(bill))
        with self.assertRaises(agent.HubUnavailable):self.sync()
        state=NodeState.objects.get();self.assertIn('No connection',state.last_error);self.assertGreater(engine.pending(state),0)
        self.assertFalse(Document.objects.using('hub').filter(pk=sale.pk).exists())
        self.wire.offline=False;self.sync()
        self.assertEqual(engine.pending(NodeState.objects.get()),0)
        for doc in (bill,sale):
            here=LedgerEntry.objects.filter(document=doc)
            self.assertEqual(self.hub_totals(doc),(sum(r.debit for r in here),sum(r.credit for r in here),here.count()))
        device=Device.objects.using('hub').get(pk=self.device_of(bill).pk)
        self.assertEqual((device.status,device.sale_id),('Sold',sale.pk))
        self.assertEqual(Conflict.objects.using('hub').count(),0)

    def test_pushing_the_same_work_again_changes_nothing(self):
        bill=self.buy();self.sync()
        before=LedgerEntry.objects.using('hub').count()
        NodeState.objects.update(pushed_through=0)
        self.sync()
        self.assertEqual(LedgerEntry.objects.using('hub').count(),before)
        self.assertEqual(Conflict.objects.using('hub').count(),0)

    def test_an_answer_lost_mid_push_is_retried_without_duplicates(self):
        bill=self.buy()
        self.wire.fail_after_push=True
        with self.assertRaises(agent.HubUnavailable):self.sync()
        self.wire.fail_after_push=False;self.sync()
        self.assertEqual(Document.objects.using('hub').filter(pk=bill.pk).count(),1)
        self.assertEqual(LedgerEntry.objects.using('hub').filter(document_id=bill.pk).count(),LedgerEntry.objects.filter(document=bill).count())

    def test_cloud_changes_come_down_to_the_laptop(self):
        with on_hub():
            Product.objects.filter(pk=self.product.pk).update(selling_price=D('71000'),modified=timezone.now())
        self.sync()
        self.assertEqual(Product.objects.get(pk=self.product.pk).selling_price,D('71000'))

    def test_a_phone_reserved_online_and_sold_offline_is_held_for_the_owner(self):
        bill=self.buy();self.sync();device=self.device_of(bill)
        with on_hub():
            Device.objects.filter(pk=device.pk).update(status='Reserved')       # the website reserved it
        sale=self.sell(device)                                                  # meanwhile the shop sold it for cash
        self.sync()
        conflict=Conflict.objects.using('hub').get()
        self.assertIn('also changed in the cloud',' '.join(conflict.reasons))
        self.assertEqual(Device.objects.using('hub').get(pk=device.pk).status,'Reserved')
        self.assertEqual(Device.objects.get(pk=device.pk).status,'Sold')        # the laptop keeps its sale meanwhile
        owner=self.user
        engine.resolve(conflict,None,'shop','hub')
        self.assertEqual(Device.objects.using('hub').get(pk=device.pk).status,'Sold')
        self.assertTrue(Document.objects.using('hub').filter(pk=sale.pk,status='Submitted').exists())

    def test_keeping_the_cloud_version_undoes_the_laptop_change_at_the_next_sync(self):
        bill=self.buy();self.sync();device=self.device_of(bill)
        with on_hub():Device.objects.filter(pk=device.pk).update(asking_price=D('99000'))
        Device.objects.filter(pk=device.pk).update(asking_price=D('55000'))
        self.sync()
        conflict=Conflict.objects.using('hub').get()
        engine.resolve(conflict,None,'cloud','hub');self.sync()
        self.assertEqual(Device.objects.get(pk=device.pk).asking_price,D('99000'))

    def test_work_dated_inside_a_month_the_owner_locked_is_held(self):
        with on_hub():Branch.objects.filter(pk=self.branch.pk).update(lock_through=self.today)
        bill=self.buy()                                                   # the laptop had not heard of the lock yet
        self.sync()
        conflict=Conflict.objects.using('hub').get()
        self.assertIn('locked',' '.join(conflict.reasons))
        self.assertFalse(Document.objects.using('hub').filter(pk=bill.pk).exists())
        self.assertEqual(Branch.objects.get(pk=self.branch.pk).lock_through,self.today)   # and the lock came down

    def test_the_same_imei_bought_in_the_cloud_and_on_the_laptop_is_held(self):
        number=imei(555)
        with on_hub():
            other=Device.objects.create(code='HUB-PHONE',product=self.product,warehouse=self.destination,branch=self.other,status='Available',purchase_cost=1)
            IMEI.objects.create(number=number,device=other,slot=1)
        bill=self.buy(number=555)
        self.sync()
        self.assertEqual(Conflict.objects.using('hub').count(),1)
        self.assertEqual(IMEI.objects.using('hub').get(number=number).device_id,other.pk)

    def test_a_new_variant_typed_in_two_places_becomes_one_product(self):
        spec=dict(brand='Nothing',model='Phone 3',storage='256GB',colour='White')
        with on_hub():hub_product=catalog_entry.resolve_variant(spec)
        here=catalog_entry.resolve_variant(spec)
        self.assertEqual(here.pk,hub_product.pk)
        self.sync()
        self.assertEqual(Product.objects.using('hub').filter(model__name='Phone 3').count(),1)
        self.assertEqual(Conflict.objects.using('hub').count(),0)

    def test_shared_settings_cannot_be_changed_on_the_laptop(self):
        with self.assertRaisesMessage(DatabaseError,'COVISON_CLOUD_ONLY'):
            Branch.objects.filter(pk=self.branch.pk).update(name='Renamed offline')

    def test_later_work_on_a_held_record_waits_behind_it(self):
        bill=self.buy();self.sync();device=self.device_of(bill)
        with on_hub():Device.objects.filter(pk=device.pk).update(status='Reserved')
        self.sell(device);self.sync()
        Device.objects.filter(pk=device.pk).update(faults='Screen scratch');self.sync()
        reasons=[c.reasons for c in Conflict.objects.using('hub').order_by('created')]
        self.assertEqual(len(reasons),2);self.assertIn('Waits for an earlier unresolved conflict on the same record.',reasons[1])
        engine.resolve(Conflict.objects.using('hub').order_by('created').first(),None,'shop','hub')
        self.assertEqual(Conflict.objects.using('hub').filter(status=Conflict.OPEN).count(),0)
        self.assertEqual(Device.objects.using('hub').get(pk=device.pk).faults,'Screen scratch')

    def test_a_whole_offline_day_reaches_the_cloud_with_matching_books(self):
        """The counter's usual work, all without internet: nothing is refused, nothing conflicts, the books agree."""
        self.wire.offline=True
        money.record_voucher(self.user,self.branch,'capital',Account.objects.get(code='COA-3100'),D(500000),'Cash',self.today,'Float')
        phones=[self.device_of(self.buy()) for _ in range(4)]
        # POS sale at the counter, as the shop's own login
        here=type(self.login).objects.get(pk=self.login.pk)       # the laptop's copy (its own password column)
        self.client.force_login(here);me=shopdefaults.person(here,self.branch)
        payload=dict(request_key=str(uuid.uuid4()),branch=str(self.branch.pk),customer_name='Counter Customer',customer_phone='0300 1234567',seller=str(me.pk),handler=str(me.pk),
                     items=[dict(product=str(phones[0].product_id),device=str(phones[0].pk),warehouse=str(phones[0].warehouse_id),quantity=1,rate='61000',pta_status='PTA')],
                     adjustments=dict(discount_type='amount',discount_value='0',tax_rule='',tax_version=''),payments=[dict(mode='Cash',amount='61000')])
        answer=self.client.post(reverse('pos_checkout'),json.dumps(payload),content_type='application/json')
        self.assertEqual(answer.status_code,200,answer.get('Location'))
        # a credit sale, later part-paid
        credit=easy_retail.sale(dict(branch=self.branch,party_name='Credit Customer',phone=phones[1],price=70000,pta_status='PTA',staff=self.seller,handler=self.handler,
                                     posting_date=self.today,payment_status='unpaid',method='Cash'),self.user)
        money.record_party_payment(self.user,credit.customer,D(30000),'Cash',self.today)
        # a sale that comes back
        sold=self.sell(phones[2]);s.submit(returns.create(sold,self.user,[str(phones[2].pk)],'Opened box','Changed mind'),self.user)
        # an expense, accessories bought, a phone written off
        money.record_voucher(self.user,self.branch,'expense',Account.objects.get(code='COA-6100'),D(8000),'Cash',self.today,'Generator diesel')
        accessories.post(dict(branch=self.branch,warehouse=self.warehouse,staff=self.buyer,handler=self.handler,posting_date=self.today,party_name='Cable Supplier',payment_status='full',method='Cash'),
                         [dict(name='USB-C cable',category='Other',qty=10,cost=D(300),price=D(600),brand='')],self.user)
        stock_adjust.post(dict(branch=self.branch,warehouse=self.warehouse,posting_date=self.today,reason='Damaged',phone=phones[3]),self.user)
        self.assertGreater(engine.pending(NodeState.objects.get()),0)
        self.wire.offline=False;self.sync()
        self.assertEqual([c.reasons for c in Conflict.objects.using('hub')],[])
        def books(using):
            rows=LedgerEntry.objects.using(using).filter(branch_id=self.branch.pk)
            return rows.count(),sum(r.debit for r in rows),sum(r.credit for r in rows)
        self.assertEqual(books('hub'),books('default'))
        self.assertEqual(set(Device.objects.using('hub').filter(branch_id=self.branch.pk).values_list('pk','status')),
                         set(Device.objects.filter(branch_id=self.branch.pk).values_list('pk','status')))
        self.assertEqual(engine.pending(NodeState.objects.get()),0)


class WebsiteGate(TestCase):
    """The cloud does not sell a shop's phones online while that shop's laptop is out of touch."""
    def test_a_silent_laptop_hides_its_shops_stock_from_online_orders_and_the_website(self):
        from shop import online_orders as oo
        transfer_setup(self)
        for n,branch in enumerate((self.branch,self.other)):
            device=Device.objects.create(code=f'GATE-{n}',product=self.product,warehouse=Warehouse.objects.filter(branch=branch).first(),branch=branch,
                                         status='Available',condition='Good',pta_status='PTA',device_type='Used',asking_price=D('50000'),purchase_cost=1)
        Product.objects.filter(pk=self.product.pk).update(online_visible=True)
        def offered():return set(oo.phone_pool(self.product,'Good','PTA','Used',D('50000')))
        def listed():
            from django.db import connection
            with connection.cursor() as c:
                c.execute('SELECT COALESCE(SUM(quantity),0) FROM web_variants WHERE product_id=%s',[self.product.pk]);return int(c.fetchone()[0])
        self.assertEqual(offered(),{self.branch.pk,self.other.pk});self.assertEqual(listed(),2)
        node=Node.objects.create(node_no=1,branch=self.branch,name='Laptop',key_hash='k'*64,last_seen=timezone.now())
        self.assertEqual(offered(),{self.branch.pk,self.other.pk});self.assertEqual(listed(),2)
        Node.objects.filter(pk=node.pk).update(last_seen=timezone.now()-timedelta(minutes=20))
        self.assertEqual(offered(),{self.other.pk});self.assertEqual(listed(),1)


class LaptopSignIn(TestCase):
    """Signing in on a laptop: checked by the cloud when online, by the hash kept from that check when offline."""
    def setUp(self):
        from unittest import mock
        from django.test import override_settings
        transfer_setup(self)
        self.person=shop_accounts.create_login(self.branch,'counter@shop.test','Strong-pass-for-CS-77!')
        self.person.set_unusable_password();self.person.save()          # as in the cloud: Supabase holds the password
        self.identity=SupabaseIdentity.objects.create(auth_id=uuid.uuid4(),user=self.person)
        NodeState.objects.create(pk=1,node_no=1,branch_id=self.branch.pk)
        patcher=override_settings(COVISON_NODE=True,SUPABASE_INTERNAL_URL='http://auth.test');patcher.enable();self.addCleanup(patcher.disable)
        self.sign_in=mock.patch('shop.supabase.sign_in');self.provider=self.sign_in.start();self.addCleanup(self.sign_in.stop)
    def attempt(self,password='Strong-pass-for-CS-77!',username='counter@shop.test'):
        from sync.node_auth import NodeBackend
        return NodeBackend().authenticate(None,username=username,password=password)
    def online(self):self.provider.side_effect=None;self.provider.return_value={'user':{'id':str(self.identity.auth_id)}}
    def offline(self):
        from shop.supabase import ProviderUnavailable
        self.provider.side_effect=ProviderUnavailable('down')
    def test_first_sign_in_must_be_online_then_offline_works(self):
        self.offline();self.assertIsNone(self.attempt())
        self.online();self.assertEqual(self.attempt(),self.person)
        self.offline();self.assertEqual(self.attempt(),self.person)
        self.assertIsNone(self.attempt(password='wrong-password-1'))
    def test_a_password_the_cloud_rejects_is_refused_even_if_this_laptop_knew_it(self):
        from django.core.exceptions import ValidationError
        self.online();self.attempt()
        self.provider.side_effect=ValidationError('rejected')
        self.assertIsNone(self.attempt())
    def test_another_shops_login_cannot_sign_in_on_this_laptop(self):
        other=shop_accounts.create_login(self.other,'other@shop.test','Strong-pass-for-OS-77!')
        self.assertIsNone(self.attempt(username='other@shop.test',password='Strong-pass-for-OS-77!'))
    def test_a_disabled_shop_login_is_refused(self):
        self.online();self.attempt()
        Staff.objects.filter(user=self.person).update(enabled=False)
        self.offline();self.assertIsNone(self.attempt())


class Screens(TestCase):
    """The owner's laptop page and conflict review in the cloud; the status page and badge on a laptop."""
    def setUp(self):
        transfer_setup(self)
        self.node=Node.objects.create(node_no=1,branch=self.branch,name='Counter laptop',key_hash='k'*64,last_seen=timezone.now())
        self.client.force_login(self.user)
    def test_owner_sees_laptops_and_resolves_a_conflict(self):
        doc=Document.objects.create(code='SALE-X',kind='Sales Invoice',branch=self.branch,status='Submitted',total=D('5000'))
        rows=[{'tbl':'shop_document','pk':str(doc.pk),'seq':1,'op':'U','fields':engine.serialize(doc)}]
        conflict=Conflict.objects.create(node=self.node,bundle=1,rows=rows,reasons=['Test reason.'])
        page=self.client.get(reverse('sync_status'));self.assertContains(page,'Counter laptop');self.assertContains(page,'Test reason.')
        page=self.client.get(reverse('sync_conflict',args=[conflict.pk]));self.assertContains(page,'SALE-X');self.assertContains(page,'Apply the shop')
        self.client.post(reverse('sync_conflict',args=[conflict.pk]),{'keep':'shop'})
        conflict.refresh_from_db();self.assertEqual(conflict.status,Conflict.APPLIED)
    def test_shop_logins_cannot_open_the_owner_page(self):
        login=shop_accounts.create_login(self.branch,'screens-a','Strong-pass-for-SA-77!');self.client.force_login(login)
        self.assertEqual(self.client.get(reverse('sync_status')).status_code,403)
    def test_laptop_badge_status_page_and_cloud_only_message(self):
        from django.test import override_settings
        NodeState.objects.create(pk=1,node_no=1,branch_id=self.branch.pk,last_attempt=timezone.now(),last_error='No connection to the cloud.')
        with override_settings(COVISON_NODE=True,SYNC_HUB_URL='https://erp.example'):
            page=self.client.get(reverse('sync_status'))
            self.assertContains(page,'Offline');self.assertContains(page,'No connection to the cloud.')
            from sync.node_views import CloudOnlyMiddleware
            from django.test import RequestFactory
            request=RequestFactory().get('/settings/');request.user=self.user
            from django.contrib.messages.storage.fallback import FallbackStorage
            request.session=self.client.session;request._messages=FallbackStorage(request)
            answer=CloudOnlyMiddleware(lambda r:None).process_exception(request,DatabaseError('COVISON_CLOUD_ONLY shop_shopsettings'))
            self.assertEqual(answer.status_code,409);self.assertIn('shop settings records',answer.content.decode())
