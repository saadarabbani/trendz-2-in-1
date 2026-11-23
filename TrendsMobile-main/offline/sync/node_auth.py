"""Signing in on a shop laptop, with or without internet.

Online: the password is checked by the cloud's Supabase Auth (as in the cloud ERP), and a local hash of it is kept on
this laptop. Offline: the local hash is checked. So a person must sign in once online on a laptop before they can sign
in there offline. A login the owner disables reaches the laptop with the next sync and is refused from then on.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import ValidationError
from django.db import connection


def _keep_password(user,password):
    """Store the verified password's hash here only (auth_user is not pushed, and the cloud copy keeps its own column)."""
    user.set_password(password)
    with connection.cursor() as cursor:
        cursor.execute('UPDATE auth_user SET password=%s WHERE id=%s',[user.password,user.pk])


class NodeBackend(ModelBackend):
    def authenticate(self,request,username=None,password=None,**kwargs):
        if not username or not password:return None
        User=get_user_model()
        user=User._default_manager.filter(username__iexact=username).first()
        if user is None or not self.user_can_authenticate(user):return None
        if not self._may_work_here(user):return None
        if settings.SUPABASE_INTERNAL_URL and hasattr(user,'supabase_identity'):
            from shop import supabase
            try:
                result=supabase.sign_in(username,password)
            except supabase.ProviderUnavailable:
                return user if user.check_password(password) else None      # offline: the hash kept from the last online sign-in
            except ValidationError:
                return None                                                  # the cloud said no: never fall back
            if str(user.supabase_identity.auth_id)!=str(result.get('user',{}).get('id')):return None
            _keep_password(user,password)
            return user
        return user if user.check_password(password) else None

    def _may_work_here(self,user):
        """The owner, or a login of this laptop's shop."""
        if user.is_superuser:return True
        from sync.models import NodeState
        state=NodeState.objects.filter(pk=1).first()
        person=getattr(user,'staff',None)
        return bool(person and person.enabled and person.branch.enabled and (state is None or person.branch_id==state.branch_id))
