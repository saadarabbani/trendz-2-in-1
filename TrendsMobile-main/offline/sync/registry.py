"""Which tables travel, and in which direction.

PUSH  - shop data. A shop laptop writes these offline and pushes them to the hub; the hub sends them on to every other laptop.
CLOUD - shared settings and owner data. Changed only in the cloud ERP. A laptop receives them and refuses to change them
        locally (the database raises COVISON_CLOUD_ONLY), so nothing typed offline can be silently lost.
LOCAL - never synced (per-machine bookkeeping).
Every model of the shop app must be in exactly one list; tests_sync checks this.
"""
from django.apps import apps

PUSH=('shop.Document','shop.Line','shop.LedgerEntry','shop.StockMovement','shop.Installment','shop.Device','shop.IMEI',
      'shop.Party','shop.Audit','shop.Product','shop.PhoneModel','shop.Brand',
      'shop.OnlineOrderAck')      # a shop marking an online order as seen
CLOUD=('shop.Branch','shop.Warehouse','shop.Till','shop.Role','shop.Staff','auth.User','shop.SupabaseIdentity','shop.ProductImage',
       'shop.Account','shop.ShopSettings','shop.SalesTaxRule','shop.FixedAsset','shop.Deferral','shop.Loan','shop.StaffRate',
       'shop.OnlineStaff','shop.OnlineOrder','shop.OnlineSettings','shop.OnlineDeliveryRule','shop.OnlinePaymentAccount',
       'shop.OnlineOrderEvent','shop.OnlineOrderLine','shop.OnlineAllocation')
LOCAL=('shop.RequestKey','shop.SourceRecord','shop.ImportRun')
# Catalogue rows any shop may create while buying stock. They are not tied to one shop.
SHARED=('shop.Product','shop.PhoneModel','shop.Brand')
# Columns a laptop keeps for itself when the cloud copy of a row arrives (the offline sign-in password).
KEEP_LOCAL={'auth.User':('password','last_login')}


def model(label):return apps.get_model(label)
def labels():return PUSH+CLOUD
def models():return [model(label) for label in labels()]
def label_of(m):return m._meta.label
def by_table():return {m._meta.db_table:m for m in models()}
def is_push(m):return label_of(m) in PUSH
def is_shared(m):return label_of(m) in SHARED
