"""Which shops' stock the website may sell right now.

A shop whose laptop has not been in touch for SYNC_STALE_MINUTES may have sold a phone the cloud has not heard about,
so the website does not reserve that shop's stock until the laptop is back. Shops without a laptop (working in the
cloud ERP directly) are always sellable. On a laptop itself this is never consulted."""
from datetime import timedelta
from django.conf import settings
from django.db.models import Q
from django.utils import timezone


def offline_branch_ids():
    if settings.COVISON_NODE:return set()
    from .models import Node
    cutoff=timezone.now()-timedelta(minutes=settings.SYNC_STALE_MINUTES)
    return set(Node.objects.filter(enabled=True).filter(Q(last_seen__isnull=True)|Q(last_seen__lt=cutoff))
               .values_list('branch_id',flat=True))


def require_synced(branch):
    """Before the owner locks a shop's month or closes its year in the cloud: that shop's laptop must have sent
    everything. Otherwise sales made offline would arrive inside the locked period and be held as conflicts."""
    from django.core.exceptions import ValidationError
    from .models import Conflict,Node
    if settings.COVISON_NODE:raise ValidationError('Lock periods and close years in the cloud ERP, not on a shop laptop.')
    cutoff=timezone.now()-timedelta(minutes=settings.SYNC_STALE_MINUTES)
    for node in Node.objects.filter(branch_id=branch.pk,enabled=True):
        if not node.last_seen or node.last_seen<cutoff:
            when=f'has not been in touch since {timezone.localtime(node.last_seen):%d %b %Y %H:%M}' if node.last_seen else 'has never synced'
            raise ValidationError(f'{node.name} for {branch.name} {when}. It may hold sales made offline. Lock this period after it reconnects and syncs.')
        if node.pending:raise ValidationError(f'{node.name} for {branch.name} still has {node.pending} changes to send. Try again in a minute.')
    if Conflict.objects.filter(node__branch_id=branch.pk,status=Conflict.OPEN).exists():
        raise ValidationError(f'{branch.name} has sync conflicts to review first (Shop laptops page).')
