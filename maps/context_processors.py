def user_role(request):
    if request.user.is_authenticated:
        profile = getattr(request.user, 'profile', None)
        return {
            'is_manager': profile.is_manager if profile else False,
            'is_provider': profile.is_provider if profile else False,
        }
    return {'is_manager': False, 'is_provider': False}


def organization(request):
    """Current org name for the nav, plus the org list for superusers
    so they can switch (each switch is audited). Also the current org's
    appointment types for the CRM/Daily/Dashboard dropdowns (the default
    three when there is no org), read from the same row: no extra query."""
    from . import appointment_types
    from .models import Organization

    ctx = {'current_org': None, 'switchable_orgs': []}
    org_id = getattr(request, 'organization_id', None)
    if org_id:
        ctx['current_org'] = Organization.objects.filter(id=org_id).first()
    types = appointment_types.normalize(
        ctx['current_org'].appointment_types if ctx['current_org'] else None)
    ctx['appointment_types'] = types
    ctx['appointment_types_by_label'] = appointment_types.sorted_by_label(types)
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and user.is_superuser:
        ctx['switchable_orgs'] = list(Organization.objects.filter(is_active=True).order_by('name'))
    return ctx
