"""The laptop's side of the conversation with the hub: provision once, then push and pull whenever online."""
import json
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from django.conf import settings
from django.db import connection,transaction
from django.utils import timezone
from . import engine,registry,triggers
from .models import Change,Held,NodeState

ID_RANGE=10**12        # laptop n numbers new rows from n * 10^12, so integer ids never collide with the hub or another laptop


class HubUnavailable(Exception):pass


class Hub:
    """Plain HTTPS client. `transport` lets tests call the hub's views in-process."""
    def __init__(self,url=None,key=None,transport=None):
        self.url=(url or settings.SYNC_HUB_URL).rstrip('/');self.key=key or settings.SYNC_NODE_KEY;self.transport=transport
    def call(self,path,query=None,payload=None):
        if self.transport:return self.transport(path,query,payload,self.key)
        url=self.url+path+('?'+urlencode(query) if query else '')
        body=json.dumps(payload).encode() if payload is not None else None
        request=Request(url,data=body,method='POST' if body else 'GET',headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
        try:
            with urlopen(request,timeout=60) as response:return json.loads(response.read())
        except HTTPError as error:
            if error.code in (401,403):raise HubUnavailable('The cloud refused this laptop’s sync key.') from None
            raise HubUnavailable(f'The cloud answered HTTP {error.code}.') from None
        except (URLError,TimeoutError,OSError,ValueError):raise HubUnavailable('No connection to the cloud.') from None


def _bigint_tables():
    """Tables a laptop adds rows to that are numbered by a counter. Cloud-only tables (users, roles) are never added to here."""
    for m in registry.models():
        if registry.is_push(m) and m._meta.pk.get_internal_type()=='BigAutoField':yield m


def set_id_ranges(node_no,using='default'):
    """Move this laptop's integer id counters into its own range (never moved backwards)."""
    start=node_no*ID_RANGE
    with connection.cursor() as cursor:
        for m in _bigint_tables():
            cursor.execute('SELECT pg_get_serial_sequence(%s,%s)',[m._meta.db_table,m._meta.pk.column])
            sequence=cursor.fetchone()[0]
            if not sequence:continue
            cursor.execute(f'SELECT GREATEST(%s,(SELECT COALESCE(MAX({connection.ops.quote_name(m._meta.pk.column)}),0)+1 FROM {connection.ops.quote_name(m._meta.db_table)}))',[start])
            cursor.execute('SELECT setval(%s,%s,false)',[sequence,cursor.fetchone()[0]])


def provision(hub,stdout=None):
    """Turn this empty local database into a copy of the cloud for one shop. Replaces every synced table here."""
    say=stdout.write if stdout else (lambda *_:None)
    hello=hub.call('/api/sync/hello/')
    tables=registry.by_table()
    with transaction.atomic():
        engine.set_origin('hub','default')
        names=', '.join(connection.ops.quote_name(t) for t in list(tables)+['sync_change','sync_held','sync_node_state'])
        with connection.cursor() as cursor:cursor.execute(f'TRUNCATE {names} CASCADE')
        for tbl in hello['tables']:
            if tbl not in tables:continue
            after=None;count=0
            while True:
                page=hub.call('/api/sync/snapshot/',{'table':tbl,**({'after':after} if after else {})})
                for row in page['rows']:engine.write(row,'default',keep_local=True)
                count+=len(page['rows'])
                if not page['rows'] or not page['more']:break
                after=page['rows'][-1]['pk']
            say(f'  {tbl}: {count}\n')
        engine.check_constraints('default')
        set_id_ranges(hello['node_no'])
        NodeState.objects.create(pk=1,node_no=hello['node_no'],branch_id=hello['branch'],pulled_through=hello['cursor'],
                                 pushed_through=Change.objects.order_by('-seq').values_list('seq',flat=True).first() or 0)
    triggers.install('node')
    return hello


def push(hub,state):
    sent=0
    while True:
        bundles,through=engine.read('default',state.pushed_through,local_only=True)
        if through<=state.pushed_through and not bundles:return sent
        held={f'{h.tbl}:{h.row_key}':h.hub_seq for h in Held.objects.all()}
        answer=hub.call('/api/sync/push/',payload={'base':state.pulled_through,'held':held,'bundles':bundles,'through':through,
                                                    'pending':engine.pending(state)})
        accepted=int(answer['accepted_through'])
        keys={(r['tbl'],r['pk']) for b in bundles for r in b['rows']}
        with transaction.atomic():
            for tbl,pk in keys:Held.objects.filter(tbl=tbl,row_key=pk).delete()
            NodeState.objects.filter(pk=state.pk).update(pushed_through=accepted,hub_conflicts=answer.get('conflicts',0))
            Change.objects.filter(seq__lte=accepted).delete()          # the cloud has them; keep the local log short
        state.pushed_through=accepted;sent+=len(bundles)
        if not bundles:return sent


def pull(hub,state):
    got=0
    while True:
        answer=hub.call('/api/sync/pull/',{'since':state.pulled_through,'pending':engine.pending(state)})
        engine.receive_pull(state,answer['bundles'],int(answer['through']))
        NodeState.objects.filter(pk=state.pk).update(hub_conflicts=answer.get('conflicts',0))
        got+=len(answer['bundles'])
        if not answer['bundles']:return got


def cycle(hub):
    """One round: push first (so the hub sees our sales before we accept its view of the same rows), then pull."""
    state=NodeState.objects.get(pk=1)
    NodeState.objects.filter(pk=1).update(last_attempt=timezone.now())
    try:
        sent=push(hub,state);got=pull(hub,state)
    except HubUnavailable as error:
        NodeState.objects.filter(pk=1).update(last_error=str(error)[:300]);raise
    NodeState.objects.filter(pk=1).update(last_success=timezone.now(),last_error='')
    return sent,got
