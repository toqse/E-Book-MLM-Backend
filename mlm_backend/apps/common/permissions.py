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
    Sticky gate for MLM placement-related user actions.

    Passes when the user has ever been admin-approved (User.kyc_first_approved_at
    is set). Subsequent edits that flip kyc_status back to PENDING do NOT revoke
    access, so a member who was previously approved keeps using the platform
    while the latest re-submission is under review.
    """

    message = "Complete compliance verification (admin-approved) to access placements."

    def has_permission(self, request, view):
        u = getattr(request, "user", None)
        if not u or not getattr(u, "is_authenticated", False):
            return False
        try:
            return bool(getattr(u, "kyc_first_approved_at", None))
        except Exception:
            return False


def require_kyc_verified_and_compliant(request):
    """
    Envelope-friendly sticky KYC guard.

    Returns None (allowed) when the user has ever been admin-approved
    (User.kyc_first_approved_at is set). Otherwise returns a 401/403 DRF
    Response with an informative message based on how far along the user is
    in the compliance flow (no profile yet vs. profile submitted/under review).
    """

    from apps.common.responses import envelope_response
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

    # Sticky pass: once approved at least once, never revoke access here even
    # if a later re-submission flipped kyc_status back to PENDING.
    if getattr(u, "kyc_first_approved_at", None):
        return None

    if not MemberComplianceProfile.objects.filter(user=u).exists():
        return envelope_response(
            None,
            message="Submit compliance details and wait for admin verification to access placements.",
            success=False,
            errors={"detail": "compliance_profile_required"},
            status=403,
        )
    return envelope_response(
        None,
        message="Complete compliance verification (admin-approved) to access placements.",
        success=False,
        errors={"detail": "kyc_required"},
        status=403,
    )
