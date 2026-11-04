from django.urls import path

from . import views

urlpatterns = [
    path("admin/integrations/", views.Overview.as_view(), name="admin-integrations"),
    path("admin/integrations/webhooks/", views.Webhooks.as_view(), name="admin-integrations-webhooks"),
    path("admin/integrations/outbox/", views.Outbox.as_view(), name="admin-integrations-outbox"),
    path("admin/integrations/outbox/<uuid:pk>/retry/", views.OutboxRetry.as_view(), name="admin-integrations-outbox-retry"),
]
