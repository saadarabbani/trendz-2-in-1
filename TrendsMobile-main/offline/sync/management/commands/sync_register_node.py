"""Run on the hub (the cloud ERP's database): register a shop laptop and print its sync key once."""
import secrets
from django.core.management.base import BaseCommand,CommandError
from django.db.models import Max
from shop.models import Branch
from sync.hub_views import key_hash
from sync.models import Node


class Command(BaseCommand):
    help='Register a shop laptop for offline work. Prints its sync key once; store it only on that laptop.'
    def add_arguments(self,parser):
        parser.add_argument('--branch',required=True,help='Shop code, e.g. GUL')
        parser.add_argument('--name',default='Shop laptop')
    def handle(self,*args,branch,name,**options):
        shop=Branch.objects.filter(code=branch,is_online=False).first()
        if not shop:raise CommandError(f'No shop with code {branch}.')
        key=secrets.token_urlsafe(32)
        number=(Node.objects.aggregate(n=Max('node_no'))['n'] or 0)+1
        Node.objects.create(node_no=number,branch=shop,name=name,key_hash=key_hash(key))
        self.stdout.write(f'Registered node {number} for {shop.name}.\nSYNC_NODE_KEY={key}\n')
