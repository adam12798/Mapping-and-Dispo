from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('api/leads/', views.leads_api, name='leads_api'),
    path('sms/', views.sms_webhook, name='sms_webhook'),
]
