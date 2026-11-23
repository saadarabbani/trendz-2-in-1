"""Screens: the laptop's sync badge and status page, and the owner's view of every laptop and its conflicts (cloud)."""
from datetime import timedelta
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import DatabaseError
from django.shortcuts import get_object_or_404,redirect,render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from . import engine,registry
from .models import Conflict,Node,NodeState


def node_state():
    if not settings.COVISON_NODE:return None
    try:return NodeState.objects.select_related('branch').filter(pk=1).first()
    except DatabaseError:return None


def status(request):
    """Template context: the badge in the header of every page on a laptop."""
    if not settings.COVISON_NODE or not getattr(request,'user',None) or not request.user.is_authenticated:return {}
    state=node_state()
    if state is None:return {'sync_badge':{'label':'Not connected to the cloud yet','error':True}}
    waiting=engine.pending(state)
    failed=bool(state.last_error) and state.last_attempt and (not state.last_success or state.last_attempt>=state.last_success)
    recent=not failed and state.last_success and timezone.now()-state.last_success<timedelta(minutes=2)
    if recent and not waiting:label='Synced with the cloud'
    elif recent:label=f'Syncing · {waiting} waiting'
    else:label=f'Offline · {waiting} waiting' if waiting else 'Offline · all work saved here'
    return {'sync_badge':{'label':label,'error':bool(state.hub_conflicts),'conflicts':state.hub_conflicts}}


class CloudOnlyMiddleware:
    """On a laptop, a change to cloud-owned data is refused by the database. Explain it instead of a server error."""
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):return self.get_response(request)
    def process_exception(self,request,exception):
        if not settings.COVISON_NODE or not isinstance(exception,DatabaseError) or 'COVISON_CLOUD_ONLY' not in str(exception):return None
        table=str(exception).split('COVISON_CLOUD_ONLY',1)[1].split()[0] if str(exception).split('COVISON_CLOUD_ONLY',1)[1].split() else ''
        m=registry.by_table().get(table)
        what=f'{m._meta.verbose_name} records' if m else 'these records'
        return render(request,'sync/cloud_only.html',{'title':'Do this in the cloud ERP','what':what,'hub':settings.SYNC_HUB_URL},status=409)


def _stale(node):
    return not node.last_seen or timezone.now()-node.last_seen>timedelta(minutes=settings.SYNC_STALE_MINUTES)


@login_required
def hub_status(request):
    if settings.COVISON_NODE:
        state=node_state()
        return render(request,'sync/node.html',{'title':'Sync with the cloud','state':state,'waiting':engine.pending(state) if state else 0,'hub':settings.SYNC_HUB_URL})
    from shop.setup import owner
    owner(request.user)
    nodes=[dict(node=n,offline=_stale(n),open=n.conflicts.filter(status=Conflict.OPEN).count()) for n in Node.objects.select_related('branch')]
    conflicts=Conflict.objects.select_related('node__branch').filter(status=Conflict.OPEN)
    return render(request,'sync/hub.html',{'title':'Shop laptops','nodes':nodes,'conflicts':conflicts})


def _describe(row):
    m=registry.by_table().get(row['tbl'])
    fields=row['fields'] or {}
    label=fields.get('code') or fields.get('name') or fields.get('number') or row['pk']
    what=fields.get('kind') or (m._meta.verbose_name.capitalize() if m else row['tbl'])
    return {'what':what,'label':label,'deleted':row['op']=='D',
            'date':fields.get('posting_date',''),'total':fields.get('total',''),'status':fields.get('status','')}


@login_required
@require_http_methods(['GET','POST'])
def conflict(request,pk):
    if settings.COVISON_NODE:return redirect('sync_status')
    from shop.setup import owner
    owner(request.user)
    item=get_object_or_404(Conflict.objects.select_related('node__branch'),pk=pk)
    if request.method=='POST' and item.status==Conflict.OPEN:
        keep=request.POST.get('keep')
        if keep in ('shop','cloud'):
            try:
                engine.resolve(item,request.user,keep,'default')
                messages.success(request,'Applied the shop’s version.' if keep=='shop' else 'Kept the cloud’s version; the laptop will be corrected at its next sync.')
            except DatabaseError as error:
                messages.error(request,'The cloud database refused the shop’s version: '+str(error).splitlines()[0][:200])
            return redirect('sync_conflict',pk=item.pk)
    shown={'shop_document':0,'shop_device':1,'shop_imei':2,'shop_party':3,'shop_product':4,'shop_phonemodel':5}
    main=sorted((r for r in item.rows if r['tbl'] in shown),key=lambda r:shown[r['tbl']])
    rows=[_describe(r) for r in main]
    return render(request,'sync/conflict.html',{'title':'Sync conflict','item':item,'rows':rows,'hidden':len(item.rows)-len(main)})
