from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('crm/', views.crm_view, name='crm'),
    path('api/leads/', views.leads_api, name='leads_api'),
    path('api/leads/<int:pk>/', views.lead_update, name='lead_update'),
    path('api/leads/bulk-delete/', views.leads_bulk_delete, name='leads_bulk_delete'),
    path('sms/', views.sms_webhook, name='sms_webhook'),
    path('reps/', views.reps_view, name='reps'),
    path('api/reps/', views.rep_create, name='rep_create'),
    path('api/reps/<int:pk>/', views.rep_update, name='rep_update'),
    path('api/reps/bulk-delete/', views.reps_bulk_delete, name='reps_bulk_delete'),
    path('api/reps/list/', views.reps_api, name='reps_api'),
    path('api/route/', views.route_api, name='route_api'),
    path('api/auto-assign/', views.auto_assign_api, name='auto_assign'),
    path('api/clear-assignments/', views.clear_assignments_api, name='clear_assignments'),
    path('api/confirm-assignments/', views.confirm_assignments_api, name='confirm_assignments'),
    path('time-off/', views.time_off_view, name='time_off'),
    path('api/time-off/', views.time_off_api, name='time_off_api'),
    path('api/time-off/by-date/', views.time_off_by_date_api, name='time_off_by_date'),
    path('api/time-off/<int:pk>/', views.time_off_update, name='time_off_update'),
]
