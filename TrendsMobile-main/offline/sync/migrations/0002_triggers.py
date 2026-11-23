"""Install the change-capture triggers as part of migrating (and remove them when migrating back), so a database
migrated back before the sync app has no trigger writing to a sync_change table that is gone."""
from django.db import migrations


def forward(apps,schema_editor):
    from sync import triggers
    triggers.install(triggers.role_for(schema_editor.connection.alias),schema_editor.connection.alias)


def backward(apps,schema_editor):
    from sync import triggers
    triggers.uninstall(schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies=[('sync','0001_initial')]
    operations=[migrations.RunPython(forward,backward)]
