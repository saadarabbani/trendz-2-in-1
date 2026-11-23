"""Offline shop nodes and the cloud hub.

Every shop laptop runs this same app on its own PostgreSQL (a "node"). The cloud ERP is the "hub". Database triggers
record every change in `Change` (see triggers.py); the node's sync agent pushes its own shop's changes up and pulls
everything else down. Rows travel whole, grouped by the transaction that wrote them, so a sale (document, lines,
ledger, stock and phone rows) always arrives complete or not at all.
"""
import uuid
from django.conf import settings
from django.db import models


class Change(models.Model):
    """One row written by one transaction. Filled only by the database trigger, never by Python code."""
    seq=models.BigAutoField(primary_key=True)
    txid=models.BigIntegerField(db_index=True)
    tbl=models.CharField(max_length=63)
    row_key=models.CharField(max_length=64)
    op=models.CharField(max_length=1)                     # I, U or D
    origin=models.CharField(max_length=40,blank=True)     # '' = written here; 'hub' or 'node:<n>' = applied by sync
    at=models.DateTimeField(auto_now_add=True)
    class Meta:
        db_table='sync_change'
        indexes=[models.Index(fields=['tbl','row_key'],name='sync_change_row')]


class NodeState(models.Model):
    """The node's own bookkeeping (a single row, pk=1). Present only on a shop laptop."""
    id=models.PositiveSmallIntegerField(primary_key=True,default=1)
    node_no=models.PositiveIntegerField()
    branch=models.ForeignKey('shop.Branch',on_delete=models.PROTECT,db_constraint=False,related_name='+')
    pushed_through=models.BigIntegerField(default=0)      # local Change.seq already accepted by the hub
    pulled_through=models.BigIntegerField(default=0)      # hub Change.seq already applied here
    last_success=models.DateTimeField(null=True,blank=True)
    last_attempt=models.DateTimeField(null=True,blank=True)
    last_error=models.CharField(max_length=300,blank=True)
    hub_conflicts=models.PositiveIntegerField(default=0)  # open conflicts for this shop, as last reported by the hub
    class Meta:db_table='sync_node_state'


class Held(models.Model):
    """A hub change this node did not apply because the same row had local changes not yet pushed. The push tells
    the hub about it, so the hub can see both sides changed the row and raise a conflict instead of overwriting."""
    tbl=models.CharField(max_length=63)
    row_key=models.CharField(max_length=64)
    hub_seq=models.BigIntegerField()
    class Meta:
        db_table='sync_held'
        constraints=[models.UniqueConstraint(fields=['tbl','row_key'],name='sync_held_row')]


class Node(models.Model):
    """A shop laptop registered with the hub (lives on the hub)."""
    node_no=models.PositiveIntegerField(unique=True)
    branch=models.ForeignKey('shop.Branch',on_delete=models.PROTECT,related_name='sync_nodes')
    name=models.CharField(max_length=120)
    key_hash=models.CharField(max_length=64,unique=True,editable=False)
    enabled=models.BooleanField(default=True)
    created=models.DateTimeField(auto_now_add=True)
    last_seen=models.DateTimeField(null=True,blank=True)
    last_push=models.DateTimeField(null=True,blank=True)
    pulled_through=models.BigIntegerField(default=0)
    pending=models.PositiveIntegerField(default=0)        # local changes waiting on the laptop, as last reported
    class Meta:
        db_table='sync_node'
        ordering=['node_no']
    def __str__(self):return f'{self.name} (node {self.node_no})'
    @property
    def origin(self):return f'node:{self.node_no}'


class Conflict(models.Model):
    """A pushed transaction the hub did not apply. The owner decides: apply the shop's version or keep the cloud's."""
    OPEN,APPLIED,DISCARDED='Open','Applied','Discarded'
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    node=models.ForeignKey(Node,on_delete=models.PROTECT,related_name='conflicts')
    bundle=models.BigIntegerField()                       # the first local Change.seq of the pushed transaction
    rows=models.JSONField()                               # the serialized rows exactly as pushed
    reasons=models.JSONField(default=list)
    status=models.CharField(max_length=10,default=OPEN,db_index=True)
    created=models.DateTimeField(auto_now_add=True)
    resolved=models.DateTimeField(null=True,blank=True)
    resolved_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL,null=True,blank=True,related_name='+')
    class Meta:
        db_table='sync_conflict'
        ordering=['created']
        constraints=[models.UniqueConstraint(fields=['node','bundle'],name='sync_conflict_once')]
    def keys(self):return {(r['tbl'],r['pk']) for r in self.rows}


class Applied(models.Model):
    """Pushed transactions the hub has already handled (applied or parked), so a retried push changes nothing."""
    node=models.ForeignKey(Node,on_delete=models.CASCADE)
    bundle=models.BigIntegerField()
    at=models.DateTimeField(auto_now_add=True)
    class Meta:
        db_table='sync_applied'
        constraints=[models.UniqueConstraint(fields=['node','bundle'],name='sync_applied_once')]
