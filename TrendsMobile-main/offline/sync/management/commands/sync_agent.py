"""Run continuously on a shop laptop: push its sales and purchases, pull the cloud's changes, retry when offline."""
import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from sync import agent


class Command(BaseCommand):
    help='Keep this shop laptop and the cloud in step.'
    def add_arguments(self,parser):
        parser.add_argument('--once',action='store_true')
        parser.add_argument('--every',type=int,default=30,help='Seconds between rounds while online.')
    def handle(self,*args,once=False,every=30,**options):
        hub=agent.Hub();wait=every
        while True:
            close_old_connections()
            try:
                sent,got=agent.cycle(hub);wait=every
                if sent or got:self.stdout.write(f'{time.strftime("%H:%M:%S")} sent {sent}, received {got}')
            except agent.HubUnavailable as error:
                wait=min(wait*2,300)
                self.stdout.write(f'{time.strftime("%H:%M:%S")} {error} Next try in {wait}s.')
            except Exception as error:      # keep the shop running; the badge shows the problem
                wait=min(wait*2,300)
                from sync.models import NodeState
                NodeState.objects.filter(pk=1).update(last_error=f'Sync error: {error}'[:300])
                self.stderr.write(f'{time.strftime("%H:%M:%S")} sync error: {error!r}')
            if once:return
            time.sleep(wait)
