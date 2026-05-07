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


class IsKycVerifiedAndCompliant(BasePermission):
    """
    Gate for MLM placement-related user actions.

    Requires:
    - user.kyc_status == VERIFIED
    - MemberComplianceProfile exists for the user
    """

    message = "Complete compliance verification (admin-approved) to access placements."

    def has_permission(self, request, view):
        u = getattr(request, "user", None)
        if not u or not getattr(u, "is_authenticated", False):
            return False
        try:
            from apps.agreements.models import MemberComplianceProfile
            from apps.users.models import User

            if getattr(u, "kyc_status", None) != User.KYCStatus.VERIFIED:
                return False
            return MemberComplianceProfile.objects.filter(user=u).exists()
        except Exception:
            return False


def require_kyc_verified_and_compliant(request):
    """
    Envelope-friendly guard for endpoints that need KYC VERIFIED + compliance profile.
    Returns a DRF Response (403) when blocked, else None.
    """

    from apps.common.responses import envelope_response
    from apps.users.models import User
    from apps.agreements.models import MemberComplianceProfile

    u = getattr(request, "user", None)
    if not u or not getattr(u, "is_authenticated", False):
        return envelope_response(
            None,
            message="Authentication required",
            success=False,
            errors={"detail": "not_authenticated"},
            status=401,
        )
    if getattr(u, "kyc_status", None) != User.KYCStatus.VERIFIED:
        return envelope_response(
            None,
            message="Complete compliance verification (admin-approved) to access placements.",
            success=False,
            errors={"detail": "kyc_required"},
            status=403,
        )
    if not MemberComplianceProfile.objects.filter(user=u).exists():
        return envelope_response(
            None,
            message="Submit compliance details and wait for admin verification to access placements.",
            success=False,
            errors={"detail": "compliance_profile_required"},
            status=403,
        )
    return None
