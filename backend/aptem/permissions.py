from django.conf import settings
from rest_framework.permissions import BasePermission


LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost"}


def is_local_request(request):
    if not getattr(settings, "APTEM_LOCAL_AUTH_BYPASS", False):
        return False
    remote_addr = request.META.get("REMOTE_ADDR", "")
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",", 1)[0].strip()
    host = request.get_host().split(":", 1)[0]
    return remote_addr in LOCAL_ADDRS or forwarded_for in LOCAL_ADDRS or host in LOCAL_ADDRS


class IsAuthenticatedStaff(BasePermission):
    def has_permission(self, request, view):
        if is_local_request(request):
            return True
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)
