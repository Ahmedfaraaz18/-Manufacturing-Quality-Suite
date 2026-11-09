from rest_framework.permissions import BasePermission

from assistant.models import Role
from assistant.permissions import HasRole, IsAdministrator, IsEngineer, IsSupervisor  # noqa: F401


class IsAnyRole(HasRole):
    """Any signed-in, active user that holds one of the application roles."""

    allowed_roles = (Role.ADMINISTRATOR, Role.ENGINEER, Role.SUPERVISOR)


class IsAdministratorOrSupervisor(HasRole):
    allowed_roles = (Role.ADMINISTRATOR, Role.SUPERVISOR)


class ReadOnlyOrAdministrator(BasePermission):
    """Any role may read; only Administrators may write."""

    message = 'Only administrators may change products.'

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_active and user.role in IsAnyRole.allowed_roles):
            return False
        return request.method in ('GET', 'HEAD', 'OPTIONS') or user.role == Role.ADMINISTRATOR
