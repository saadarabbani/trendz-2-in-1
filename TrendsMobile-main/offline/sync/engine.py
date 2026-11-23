"""Reading changes as bundles and applying bundles, on either side.

A bundle is everything one database transaction wrote: [{'tbl','pk','op','seq','fields'}], identified by the first
sync_change.seq it occupies. Rows carry their current state (not a diff), so applying a bundle twice is harmless.
All functions take `using` so the tests can run a laptop and the hub side by side in one process.
"""
import json
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError,connections,transaction
from django.db.models import ManyToManyField
from django.utils import timezone
from . import registry
from .models import Change,Conflict,Held,NodeState,Applied

HUB='hub'
WINDOW=400        # changes read per round trip


def set_origin(origin,using):
    with connections[using].cursor() as cursor:cursor.execute("SELECT set_config('covison.sync_origin',%s,true)",[origin])


def _freeze(using):
    """Wait (briefly) until every transaction that is writing sync_change has finished, and hold new ones back while
    we read. Without this a transaction that took seq 10 but commits after the one that took seq 11 would be missed."""
    with connections[using].cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout='800ms'")      # below deadlock_timeout: a busy hub makes us retry, never fails a sale
        cursor.execute('LOCK TABLE sync_change IN SHARE MODE')


def serialize(obj):
    data=serializers.serialize('python',[obj])[0]
    fields={k:v for k,v in data['fields'].items() if not isinstance(obj._meta.get_field(k),ManyToManyField)}
    return json.loads(json.dumps(fields,cls=DjangoJSONEncoder))


def current(m,pk,using):
    key=m._meta.pk.to_python(pk)
    return m._base_manager.using(using).filter(pk=key).first()


def read(using,after,*,local_only=False,exclude_origin=None,limit=WINDOW,skip=frozenset()):
    """Bundles written after `after`, oldest first, and the seq to continue from.

    Rows travel with their current state, so a phone bought in one transaction and sold in the next is read already
    sold, pointing at a sale document that only the second transaction wrote. Transactions that touched a common row
    are therefore sent as one bundle, and every later transaction touching a row already in the batch is pulled in.
    `skip` holds rows not to send (the hub's side of an open conflict with this laptop)."""
    tables=registry.by_table()
    with transaction.atomic(using=using):
        _freeze(using)
        changes=Change.objects.using(using).filter(seq__gt=after)
        if local_only:changes=changes.filter(origin='')
        if exclude_origin:changes=changes.exclude(origin=exclude_origin)
        pool=list(changes.order_by('seq')[:limit*25])
        window=pool[:limit]
        if len(window)<limit:
            through=Change.objects.using(using).order_by('-seq').values_list('seq',flat=True).first() or after
            through=max(through,after)
        else:through=window[-1].seq
        if not window:return [],through
        by_tx={}
        for c in pool:by_tx.setdefault(c.txid,[]).append(c)
        chosen={c.txid for c in window};keys={(c.tbl,c.row_key) for t in chosen for c in by_tx[t]}
        grew=True
        while grew:          # every transaction touching a chosen row, until nothing more is added
            grew=False
            for txid,rows in by_tx.items():
                if txid not in chosen and any((c.tbl,c.row_key) in keys for c in rows):
                    chosen.add(txid);keys|={(c.tbl,c.row_key) for c in rows};grew=True
        parent={t:t for t in chosen}
        def root(t):
            while parent[t]!=t:parent[t]=parent[parent[t]];t=parent[t]
            return t
        owner={}
        for txid in sorted(chosen,key=lambda t:by_tx[t][0].seq):
            for c in by_tx[txid]:
                key=(c.tbl,c.row_key)
                if key in owner:parent[root(txid)]=root(owner[key])
                else:owner[key]=txid
        groups={}
        for txid in chosen:
            group=groups.setdefault(root(txid),{'id':None,'rows':{}})
            for c in by_tx[txid]:
                group['id']=c.seq if group['id'] is None else min(group['id'],c.seq)
                group['rows'][(c.tbl,c.row_key)]=max(c.seq,group['rows'].get((c.tbl,c.row_key),0))
        bundles=[]
        for group in sorted(groups.values(),key=lambda g:g['id']):
            rows=[]
            for (tbl,pk),seq in sorted(group['rows'].items(),key=lambda item:item[1]):
                m=tables.get(tbl)
                if m is None or (tbl,pk) in skip:continue
                obj=current(m,pk,using)
                rows.append({'tbl':tbl,'pk':pk,'seq':seq,'op':'D' if obj is None else 'U','fields':None if obj is None else serialize(obj)})
            if rows:bundles.append({'id':group['id'],'rows':rows})
        return bundles,through


def _brand_clash(m,row,using):
    """Brands are matched by name: two shops that both type a new brand offline get one brand, not an error."""
    if registry.label_of(m)!='shop.Brand' or not row['fields']:return None
    return m._base_manager.using(using).filter(name__iexact=row['fields']['name']).exclude(pk=m._meta.pk.to_python(row['pk'])).first()


def write(row,using,keep_local=False):
    m=registry.by_table()[row['tbl']]
    if row['op']=='D':
        m._base_manager.using(using).filter(pk=m._meta.pk.to_python(row['pk']))._raw_delete(using)
        return
    clash=_brand_clash(m,row,using)
    if clash is not None:
        if not keep_local:return          # the hub keeps its own brand of that name
        m._base_manager.using(using).filter(pk=clash.pk)._raw_delete(using)
    fields=dict(row['fields'])
    keep=registry.KEEP_LOCAL.get(registry.label_of(m),())
    if keep_local and keep:
        existing=current(m,row['pk'],using)
        mine=serializers.serialize('python',[existing])[0]['fields'] if existing is not None else {}
        for name in keep:
            if name=='password' and not str(fields.get('password') or '!').startswith('!'):continue      # the cloud's own usable password wins
            fields[name]=mine.get(name,'!' if name=='password' else None)      # Supabase login: the hash kept from this laptop's last online sign-in
    for item in serializers.deserialize('python',[{'model':registry.label_of(m).lower(),'pk':row['pk'],'fields':fields}],using=using,ignorenonexistent=True):
        item.save(using=using)


def check_constraints(using):
    """Run the deferred foreign-key checks now, so a bad bundle fails inside its own savepoint."""
    with connections[using].cursor() as cursor:cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
    defer(using)


def defer(using):
    with connections[using].cursor() as cursor:cursor.execute('SET CONSTRAINTS ALL DEFERRED')


# ---------------------------------------------------------------- hub side

def _row_branch(m,fields,bundle_rows,using):
    """The shop a pushed row belongs to, or None for shared catalogue rows."""
    if registry.is_shared(m) or not fields:return None
    if 'branch' in fields:return fields['branch']
    from shop.models import Document,Device,Warehouse
    def doc_branch(pk):
        for r in bundle_rows:
            if r['tbl']==Document._meta.db_table and r['pk']==str(pk) and r['fields']:return r['fields']['branch']
        return Document.objects.using(using).filter(pk=pk).values_list('branch_id',flat=True).first()
    if 'document' in fields:return str(doc_branch(fields['document']))
    if 'device' in fields:
        for r in bundle_rows:
            if r['tbl']==Device._meta.db_table and r['pk']==str(fields['device']) and r['fields']:return r['fields']['branch']
        return str(Device.objects.using(using).filter(pk=fields['device']).values_list('branch_id',flat=True).first())
    if 'warehouse' in fields:return str(Warehouse.objects.using(using).filter(pk=fields['warehouse']).values_list('branch_id',flat=True).first())
    return None


def _same(a,b):
    a={k:v for k,v in (a or {}).items() if k not in ('modified','created')};b={k:v for k,v in (b or {}).items() if k not in ('modified','created')}
    return a==b


def problems(node,bundle,base,held,using=HUB):
    """Why this pushed transaction cannot be applied as it is (empty list = fine)."""
    from shop.models import Branch,Document
    tables=registry.by_table();reasons=[];keys={(r['tbl'],r['pk']) for r in bundle['rows']}
    for conflict in Conflict.objects.using(using).filter(node=node,status=Conflict.OPEN):
        if conflict.keys()&keys:
            reasons.append('Waits for an earlier unresolved conflict on the same record.');break
    lock=Branch.objects.using(using).filter(pk=node.branch_id).values_list('lock_through',flat=True).first()
    for row in bundle['rows']:
        m=tables.get(row['tbl'])
        if m is None:reasons.append(f'Unknown table {row["tbl"]}.');continue
        name=m._meta.verbose_name
        if not registry.is_push(m):reasons.append(f'A shop cannot change {name} records.');continue
        since=min(base,held.get(f'{row["tbl"]}:{row["pk"]}',base+1)-1)
        if Change.objects.using(using).filter(tbl=row['tbl'],row_key=row['pk'],seq__gt=since).exclude(origin=node.origin).exists():
            now=current(m,row['pk'],using)
            if not _same(serialize(now) if now is not None else None,row['fields']):
                reasons.append(f'This {name} was also changed in the cloud (or by another shop) while this shop was offline.')
        branch=_row_branch(m,row['fields'],bundle['rows'],using)
        if branch is not None and branch!=str(node.branch_id):
            reasons.append(f'This {name} belongs to another shop.')
        if m is Document and row['fields'] and lock:
            date=row['fields']['posting_date'];before=current(m,row['pk'],using)
            unchanged=before is not None and before.status==row['fields']['status'] and before.posting_date.isoformat()==date
            if not unchanged and date<=lock.isoformat():reasons.append(f'Dated {date}, inside the period the owner locked (up to {lock:%d %b %Y}).')
    return list(dict.fromkeys(reasons))


def apply(node,bundle,using=HUB):
    """Apply one pushed transaction as the given node. Raises DatabaseError if the database refuses it."""
    with transaction.atomic(using=using):
        set_origin(node.origin,using)
        for row in bundle['rows']:write(row,using)
        check_constraints(using)


def receive_push(node,payload,using=HUB):
    """Handle a push: apply what is safe, park the rest as conflicts. Returns the seq the laptop may forget up to."""
    base=int(payload.get('base') or 0);held=payload.get('held') or {}
    for bundle in payload.get('bundles',[]):
        with transaction.atomic(using=using):
            if Applied.objects.using(using).filter(node=node,bundle=bundle['id']).exists():continue
            reasons=problems(node,bundle,base,held,using)
            if not reasons:
                try:apply(node,bundle,using)
                except DatabaseError as error:
                    defer(using)
                    reasons=['The cloud database refused it: '+str(error).splitlines()[0][:200]]
            if reasons:Conflict.objects.using(using).create(node=node,bundle=bundle['id'],rows=bundle['rows'],reasons=reasons)
            Applied.objects.using(using).create(node=node,bundle=bundle['id'])
    node.last_seen=node.last_push=timezone.now();node.pending=int(payload.get('pending') or 0)
    node.save(using=using,update_fields=['last_seen','last_push','pending'])
    return int(payload.get('through') or 0)


def resolve(conflict,user,keep,using=HUB):
    """keep='shop': apply the laptop's rows anyway. keep='cloud': discard them and send the cloud's rows back down."""
    with transaction.atomic(using=using):
        conflict=Conflict.objects.using(using).select_for_update().get(pk=conflict.pk)
        if conflict.status!=Conflict.OPEN:return conflict
        if keep=='shop':apply(conflict.node,{'id':conflict.bundle,'rows':conflict.rows},using)
        else:
            set_origin('hub',using)
            tables=registry.by_table()
            with connections[using].cursor() as cursor:
                for row in conflict.rows:
                    m=tables.get(row['tbl'])
                    exists=m is not None and current(m,row['pk'],using) is not None
                    # Re-announce the cloud's row (or its absence) so the laptop overwrites what it pushed.
                    cursor.execute('INSERT INTO sync_change (txid,tbl,row_key,op,origin,at) VALUES (txid_current(),%s,%s,%s,%s,now())',
                                   [row['tbl'],row['pk'],'U' if exists else 'D','hub'])
        conflict.status=Conflict.APPLIED if keep=='shop' else Conflict.DISCARDED
        conflict.resolved=timezone.now();conflict.resolved_by=user;conflict.save(using=using)
    # Anything parked only because it waited on this one gets another chance.
    for waiting in Conflict.objects.using(using).filter(node=conflict.node,status=Conflict.OPEN).order_by('created'):
        if waiting.reasons==['Waits for an earlier unresolved conflict on the same record.']:
            retry(waiting,using)
    return conflict


def retry(conflict,using=HUB):
    with transaction.atomic(using=using):
        conflict=Conflict.objects.using(using).select_for_update().get(pk=conflict.pk)
        bundle={'id':conflict.bundle,'rows':conflict.rows}
        others=Conflict.objects.using(using).filter(node=conflict.node,status=Conflict.OPEN).exclude(pk=conflict.pk)
        if any(o.keys()&conflict.keys() for o in others):return False
        try:
            with transaction.atomic(using=using):apply(conflict.node,bundle,using)
        except DatabaseError:return False
        conflict.status=Conflict.APPLIED;conflict.resolved=timezone.now();conflict.save(using=using)
        return True


# ---------------------------------------------------------------- laptop side

def pending(state,using='default'):
    return Change.objects.using(using).filter(seq__gt=state.pushed_through,origin='').count()


def receive_pull(state,bundles,through,using='default'):
    """Apply the hub's bundles here. A row with local changes not yet pushed is held, not overwritten."""
    for bundle in bundles:
        with transaction.atomic(using=using):
            set_origin('hub',using)
            for row in bundle['rows']:
                local=Change.objects.using(using).filter(tbl=row['tbl'],row_key=row['pk'],origin='',seq__gt=state.pushed_through).exists()
                if local:
                    Held.objects.using(using).update_or_create(tbl=row['tbl'],row_key=row['pk'],defaults={'hub_seq':row['seq']})
                    continue
                write(row,using,keep_local=True)
            check_constraints(using)
    NodeState.objects.using(using).filter(pk=state.pk).update(pulled_through=through)
    state.pulled_through=through
