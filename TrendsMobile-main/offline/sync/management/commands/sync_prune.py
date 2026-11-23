"""Run on the hub now and then (e.g. weekly): forget changes every laptop has already received."""
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db.models import Min
from django.utils import timezone
from sync.models import Applied,Change,Node


class Command(BaseCommand):
    help='Delete sync history that every registered shop laptop has already pulled.'
    def add_arguments(self,parser):parser.add_argument('--days',type=int,default=30,help='Always keep at least this many days.')
    def handle(self,*args,days=30,**options):
        cutoff=timezone.now()-timedelta(days=days)
        floor=Node.objects.filter(enabled=True).aggregate(m=Min('pulled_through'))['m']
        changes=Change.objects.filter(at__lt=cutoff)
        if floor is not None:changes=changes.filter(seq__lte=floor)
        removed,_=changes.delete()
        old,_=Applied.objects.filter(at__lt=timezone.now()-timedelta(days=max(days,90))).delete()
        self.stdout.write(f'Removed {removed} change records and {old} push receipts.')
