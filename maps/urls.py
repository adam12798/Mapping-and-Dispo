from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('crm/', views.crm_view, name='crm'),
    path('api/leads/', views.leads_api, name='leads_api'),
    path('api/leads/<int:pk>/', views.lead_update, name='lead_update'),
    path('api/leads/bulk-delete/', views.leads_bulk_delete, name='leads_bulk_delete'),
    path('sms/', views.sms_webhook, name='sms_webhook'),
]
