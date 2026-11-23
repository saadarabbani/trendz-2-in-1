"""The hub's sync API, called by the shop laptops' sync agent. Each laptop signs requests with its own key
(Authorization: Bearer <key>); the hub stores only the key's SHA-256."""
import hashlib,json
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET,require_POST
from . import engine,registry
from .models import Change,Conflict,Node

SNAPSHOT_PAGE=500


def key_hash(key):return hashlib.sha256(key.encode()).hexdigest()


def _node(request):
    header=request.headers.get('Authorization','')
    if not header.startswith('Bearer ') or len(header)<20:return None
    return Node.objects.select_related('branch').filter(key_hash=key_hash(header[7:].strip()),enabled=True).first()


def _authorized(view):
    def wrapper(request,*args,**kwargs):
        node=_node(request)
        if node is None:return JsonResponse({'error':'Unknown or disabled shop laptop.'},status=401)
        response=view(request,node,*args,**kwargs)
        response['Cache-Control']='no-store'
        return response
    return csrf_exempt(wrapper)


def _cursor(using='default'):
    with transaction.atomic(using=using):
        engine._freeze(using)
        return Change.objects.using(using).order_by('-seq').values_list('seq',flat=True).first() or 0


# The hub's answers as plain functions (the views below, and the tests' in-process hub, call these).

def do_hello(node,using='default'):
    node.last_seen=timezone.now();node.save(using=using,update_fields=['last_seen'])
    return {'node_no':node.node_no,'branch':str(node.branch_id),'branch_name':node.branch.name,'cursor':_cursor(using),
            'tables':[m._meta.db_table for m in registry.models()]}


def do_snapshot(node,table,after,using='default'):
    m=registry.by_table().get(table)
    if m is None:return None
    rows=m._base_manager.using(using).order_by('pk')
    if after:rows=rows.filter(pk__gt=m._meta.pk.to_python(after))
    page=list(rows[:SNAPSHOT_PAGE])
    return {'rows':[{'tbl':m._meta.db_table,'pk':str(obj.pk),'seq':0,'op':'U','fields':engine.serialize(obj)} for obj in page],
            'more':len(page)==SNAPSHOT_PAGE}


def do_push(node,payload,using='default'):
    through=engine.receive_push(node,payload,using)
    return {'accepted_through':through,'conflicts':Conflict.objects.using(using).filter(node=node,status=Conflict.OPEN).count()}


def do_pull(node,since,using='default',pending=None):
    held=set()
    for conflict in Conflict.objects.using(using).filter(node=node,status=Conflict.OPEN):held|=conflict.keys()
    bundles,through=engine.read(using,since,exclude_origin=node.origin,skip=held)      # the laptop keeps its version until the owner decides
    node.last_seen=timezone.now();node.pulled_through=since
    if pending is not None:node.pending=max(int(pending),0)
    node.save(using=using,update_fields=['last_seen','pulled_through','pending'])
    return {'bundles':bundles,'through':through,'conflicts':Conflict.objects.using(using).filter(node=node,status=Conflict.OPEN).count()}


@_authorized
@require_GET
def hello(request,node):
    """Everything a new laptop needs before it copies the cloud data: who it is and where to resume pulling."""
    return JsonResponse(do_hello(node))


@_authorized
@require_GET
def snapshot(request,node):
    page=do_snapshot(node,request.GET.get('table',''),request.GET.get('after'))
    if page is None:return JsonResponse({'error':'Unknown table.'},status=400)
    return JsonResponse(page)


@_authorized
@require_POST
def push(request,node):
    try:payload=json.loads(request.body)
    except ValueError:return JsonResponse({'error':'Send JSON.'},status=400)
    return JsonResponse(do_push(node,payload))


@_authorized
@require_GET
def pull(request,node):
    try:since=int(request.GET.get('since','0'))
    except ValueError:return JsonResponse({'error':'Bad cursor.'},status=400)
    try:pending=int(request.GET['pending']) if 'pending' in request.GET else None
    except ValueError:pending=None
    return JsonResponse(do_pull(node,since,pending=pending))
