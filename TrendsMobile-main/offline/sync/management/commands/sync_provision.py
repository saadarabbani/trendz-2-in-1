"""Run once on a new shop laptop: copy the cloud data into the local database. Erases the synced tables here first."""
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from sync import agent
from sync.models import NodeState


class Command(BaseCommand):
    help='Copy the cloud ERP into this laptop’s database and make it a shop node.'
    def add_arguments(self,parser):
        parser.add_argument('--force',action='store_true',help='Re-copy even though this laptop is already set up (unsynced local work is lost).')
    def handle(self,*args,force=False,**options):
        if not settings.COVISON_NODE:raise CommandError('Set COVISON_NODE=1, SYNC_HUB_URL and SYNC_NODE_KEY first.')
        state=NodeState.objects.filter(pk=1).first()
        if state and not force:raise CommandError('This laptop is already set up. Use --force only on the advice of support.')
        if state and force:
            from sync.engine import pending
            waiting=pending(state)
            if waiting:raise CommandError(f'{waiting} local changes have not reached the cloud yet. Connect and let the sync agent finish first.')
        try:hello=agent.provision(agent.Hub(),self.stdout)
        except agent.HubUnavailable as error:raise CommandError(str(error))
        self.stdout.write(self.style.SUCCESS(f'This laptop is now node {hello["node_no"]} for {hello["branch_name"]}.'))
