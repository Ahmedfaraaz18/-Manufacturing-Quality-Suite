from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .models import Role


class HasRole(BasePermission):
    """Server-side role check. Subclasses list the roles allowed; superuser status grants nothing here."""

    allowed_roles: tuple = ()
    message = 'Your role is not permitted to perform this action.'

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.role in self.allowed_roles)


class IsAdministrator(HasRole):
    allowed_roles = (Role.ADMINISTRATOR,)


class IsEngineer(HasRole):
    allowed_roles = (Role.ENGINEER,)


class IsSupervisor(HasRole):
    allowed_roles = (Role.SUPERVISOR,)


class IsEngineerOrSupervisor(HasRole):
    allowed_roles = (Role.ENGINEER, Role.SUPERVISOR)


def role_required(*roles):
    """Decorator for page views: anonymous -> login redirect, wrong role -> 403."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not user.is_active or user.role not in roles:
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
