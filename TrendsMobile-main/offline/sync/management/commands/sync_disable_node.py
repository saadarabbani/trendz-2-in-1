"""Run on the hub: stop a lost or replaced shop laptop from syncing (its key stops working at once)."""
from django.core.management.base import BaseCommand,CommandError
from sync.models import Node


class Command(BaseCommand):
    help='Disable a shop laptop so its sync key is refused.'
    def add_arguments(self,parser):parser.add_argument('node_no',type=int)
    def handle(self,*args,node_no,**options):
        if not Node.objects.filter(node_no=node_no).update(enabled=False):raise CommandError(f'No laptop number {node_no}.')
        self.stdout.write(f'Laptop {node_no} disabled.')
