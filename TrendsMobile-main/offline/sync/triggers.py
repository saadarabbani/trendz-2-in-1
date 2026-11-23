"""The database trigger that records every change in sync_change.

The hub records every write (with its origin, so a laptop is not sent its own rows back). A laptop records only what
was typed on it (origin '') and refuses local writes to cloud-owned tables. Installed after every migrate (apps.py),
so a table added later is covered too.
"""
from django.db import connections
from . import registry

FUNCTION="""
CREATE OR REPLACE FUNCTION covison_sync_capture() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE
  origin text := coalesce(current_setting('covison.sync_origin', true), '');
  k text;
BEGIN
  IF TG_ARGV[1] = 'block' THEN
    IF origin = '' THEN
      RAISE EXCEPTION 'COVISON_CLOUD_ONLY %', TG_TABLE_NAME USING ERRCODE = 'P0001',
        HINT = 'This record is shared by every shop and can only be changed in the cloud ERP.';
    END IF;
    RETURN NULL;
  END IF;
  IF TG_ARGV[1] = 'local' AND origin <> '' THEN RETURN NULL; END IF;
  IF TG_OP = 'DELETE' THEN k := to_jsonb(OLD) ->> TG_ARGV[0]; ELSE k := to_jsonb(NEW) ->> TG_ARGV[0]; END IF;
  INSERT INTO sync_change (txid, tbl, row_key, op, origin, at) VALUES (txid_current(), TG_TABLE_NAME, k, left(TG_OP, 1), origin, now());
  RETURN NULL;
END
$f$;
"""
TRIGGER='covison_sync'


def policy(m,role):
    """'all' (hub: record everything), 'local' (laptop: record what was typed here), 'block' (laptop: refuse), None."""
    if role=='hub':return 'all'
    if registry.is_push(m):return 'local'
    if registry.label_of(m) in registry.KEEP_LOCAL:return None      # the laptop's own sign-in data; the cloud copy arrives by pull
    return 'block'


def install(role,using='default'):
    """(Re)create the triggers. With no sync_change table (the database was migrated back before the sync app),
    remove them instead, so nothing writes to a table that is not there."""
    connection=connections[using]
    with connection.cursor() as cursor:
        tables=set(connection.introspection.table_names(cursor))
        if 'sync_change' not in tables:return uninstall(using)
        cursor.execute(FUNCTION)
        for m in registry.models():
            if m._meta.db_table not in tables:continue
            table=connection.ops.quote_name(m._meta.db_table)
            cursor.execute(f'DROP TRIGGER IF EXISTS {TRIGGER} ON {table}')
            rule=policy(m,role)
            if rule:
                cursor.execute(f"CREATE TRIGGER {TRIGGER} AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW "
                               f"EXECUTE FUNCTION covison_sync_capture('{m._meta.pk.column}','{rule}')")


def uninstall(using='default'):
    connection=connections[using]
    with connection.cursor() as cursor:
        tables=set(connection.introspection.table_names(cursor))
        for m in registry.models():
            if m._meta.db_table in tables:cursor.execute(f'DROP TRIGGER IF EXISTS {TRIGGER} ON {connection.ops.quote_name(m._meta.db_table)}')
        cursor.execute('DROP FUNCTION IF EXISTS covison_sync_capture()')


def role_for(using):
    from django.conf import settings
    return 'node' if settings.COVISON_NODE and using=='default' else 'hub'
