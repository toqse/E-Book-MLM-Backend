from rest_framework.permissions import BasePermission


class IsAdminRole(BasePermission):
    allowed = {"SUPER_ADMIN", "FINANCE", "SUPPORT"}

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u and u.is_authenticated and getattr(u, "role", None) in self.allowed and u.is_staff
        )


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and getattr(u, "role", None) == "SUPER_ADMIN")


class IsFinanceAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and getattr(u, "role", None) in ("SUPER_ADMIN", "FINANCE")
            and u.is_staff
        )


class IsSupportAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and getattr(u, "role", None) in ("SUPER_ADMIN", "SUPPORT")
            and u.is_staff
        )
