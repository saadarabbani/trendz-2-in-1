from django.apps import AppConfig


class SyncConfig(AppConfig):
    name='sync'
    verbose_name='Offline shops and cloud sync'
    default_auto_field='django.db.models.BigAutoField'
    def ready(self):
        from django.db.models.signals import post_migrate,pre_migrate
        pre_migrate.connect(_migrating,sender=self,dispatch_uid='covison-sync-migrating')
        post_migrate.connect(_install,sender=self,dispatch_uid='covison-sync-triggers')


def _origin(using,value):
    from django.db import connections
    with connections[using].cursor() as cursor:cursor.execute("SELECT set_config('covison.sync_origin',%s,false)",[value])


def _migrating(sender,using='default',**kwargs):
    # Data migrations run on the cloud and on every laptop alike: on a laptop they must neither be refused as
    # cloud-only changes nor queued for pushing (the cloud runs the same migration itself).
    _origin(using,'migration')


def _install(sender,using='default',**kwargs):
    from . import triggers
    _origin(using,'')
    triggers.install(triggers.role_for(using),using)
