"""Sets the active organization for each request from the logged-in user.

- Normal users: organization comes from their UserProfile.
- Superusers: may have switched into another organization for support;
  the switch lives in the session and every change is written to
  OrgSwitchAudit (see views.org_switch).
- Anonymous requests get NO organization: scoped managers return nothing.
  Unauthenticated entry points (Twilio SMS, GHL webhooks, the voice
  WebSocket, the reminder worker) each resolve an org explicitly and open
  their own org_context instead.
"""
from .tenancy import _current_org_id


class OrganizationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        org_id = None
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            profile = getattr(user, 'profile', None)
            if profile is not None:
                org_id = profile.organization_id
            if user.is_superuser:
                switched = request.session.get('active_org_id')
                if switched:
                    org_id = switched
        request.organization_id = org_id
        token = _current_org_id.set(org_id)
        try:
            return self.get_response(request)
        finally:
            _current_org_id.reset(token)
