"""Multi-tenant scoping: every owned row belongs to an Organization.

The current organization lives in a contextvar so it works in sync Django
views, the FastAPI voice WebSocket (asgiref propagates context into
sync_to_async threads), and management commands. Plain threads (e.g.
threading.Timer in fire_webhooks) do NOT inherit it — they must resolve an
org explicitly and open their own org_context.

Fail-closed: with no organization in context, the scoped default manager
returns an empty queryset. `Model.all_objects` is the explicit, greppable
escape hatch for cross-org access (caller identification by phone number,
org resolution itself, the org-switch machinery).
"""
import contextvars
from contextlib import contextmanager

from django.db import models

_current_org_id = contextvars.ContextVar('sutton_current_org_id', default=None)


def get_current_org_id():
    return _current_org_id.get()


def set_current_org(org_or_id):
    """Set the active organization. Returns a token for reset_current_org()."""
    org_id = getattr(org_or_id, 'id', org_or_id)
    return _current_org_id.set(org_id)


def reset_current_org(token):
    _current_org_id.reset(token)


@contextmanager
def org_context(org_or_id):
    token = set_current_org(org_or_id)
    try:
        yield
    finally:
        _current_org_id.reset(token)


class OrgQuerySet(models.QuerySet):
    """Stamps the active organization onto new rows so ~40 existing
    .create() call sites keep working without touching each one."""

    def create(self, **kwargs):
        if 'organization' not in kwargs and 'organization_id' not in kwargs:
            org_id = _current_org_id.get()
            if org_id is not None:
                kwargs['organization_id'] = org_id
        return super().create(**kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        org_id = _current_org_id.get()
        if org_id is not None:
            for obj in objs:
                if getattr(obj, 'organization_id', None) is None:
                    obj.organization_id = org_id
        return super().bulk_create(objs, *args, **kwargs)


class OrgScopedManager(models.Manager.from_queryset(OrgQuerySet)):
    """Default manager: only ever returns rows for the active organization.
    No active organization -> empty queryset (fail-closed)."""

    def get_queryset(self):
        qs = super().get_queryset()
        org_id = _current_org_id.get()
        if org_id is None:
            return qs.none()
        return qs.filter(organization_id=org_id)


class AllObjectsManager(models.Manager.from_queryset(OrgQuerySet)):
    """Unscoped escape hatch. Every use should be a deliberate cross-org
    operation (phone-number identification, org resolution, backfills)."""
    pass


class OrgOwnedModel(models.Model):
    organization = models.ForeignKey(
        'maps.Organization', null=True, blank=True,
        on_delete=models.PROTECT, related_name='+',
    )

    objects = OrgScopedManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True
        # Django's internal plumbing (FK traversal, one-to-one reverse access
        # like request.user.profile) must never be org-filtered.
        base_manager_name = 'all_objects'

    def save(self, *args, **kwargs):
        if self.organization_id is None:
            org_id = _current_org_id.get()
            if org_id is not None:
                self.organization_id = org_id
        super().save(*args, **kwargs)
