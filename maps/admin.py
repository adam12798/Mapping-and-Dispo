from django.contrib import admin

from .models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    """Superusers only, and only Alfred's per-organization switch: turning
    `alfred_declines` on makes Alfred decline that organization's callers
    (maps/alfred_org.py). Nothing else about an organization is edited here,
    none is added or deleted. Django's admin log records each change."""
    list_display = ('name', 'slug', 'is_active', 'is_default_inbound', 'alfred_declines')
    fields = ('name', 'slug', 'is_active', 'is_default_inbound', 'alfred_declines')
    readonly_fields = ('name', 'slug', 'is_active', 'is_default_inbound')

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
