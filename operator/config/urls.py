from django.urls import path

from mockoperator import views

urlpatterns = [
    path("operator/vend", views.vend, name="operator-vend"),
    path("operator/status/<str:reference>", views.status, name="operator-status"),
    path("operator/health", views.health, name="operator-health"),
]
