"""The website stops offering a shop's stock while that shop's laptop is offline (silent for 5 minutes), because the
shop may already have sold it without internet. Same columns as 0028, so the views are replaced in place and the
hosted api.web_* views that select from them keep working without being dropped."""
import importlib
from django.db import migrations

v4=importlib.import_module('shop.migrations.0028_web_views_v4')
OFFLINE="(SELECT n.branch_id FROM sync_node n WHERE n.enabled AND (n.last_seen IS NULL OR n.last_seen < now() - interval '5 minutes'))"
ENABLED=f"(SELECT id FROM shop_branch WHERE enabled AND NOT is_online AND id NOT IN {OFFLINE})"
VIEWS=('LISTINGS','FAMILIES','VARIANTS','VARIANT_STOCK','STOCK','UNITS','IMAGES')


def gated(sql):
    sql=sql.replace(v4.ENABLED,ENABLED).replace('b.enabled AND NOT b.is_online','b.enabled AND NOT b.is_online AND b.id NOT IN '+OFFLINE)
    return sql.replace('CREATE VIEW','CREATE OR REPLACE VIEW')


def forward(apps,schema_editor):
    for name in VIEWS:schema_editor.execute(gated(getattr(v4,name)))


def backward(apps,schema_editor):
    for name in VIEWS:schema_editor.execute(getattr(v4,name).replace('CREATE VIEW','CREATE OR REPLACE VIEW'))


class Migration(migrations.Migration):
    dependencies=[('shop','0033_pta_status_jv'),('sync','0001_initial')]
    operations=[migrations.RunPython(forward,backward)]
