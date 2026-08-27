from django.http import JsonResponse
from django.urls import path

from vending.views import ussd_view


def health(request):
    return JsonResponse({"ok": True})


urlpatterns = [
    path("ussd", ussd_view, name="ussd"),
    path("health", health, name="health"),
]
