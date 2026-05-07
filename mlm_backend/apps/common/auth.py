from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class JWTAuthenticationWithAccountStatus(JWTAuthentication):
    """
    Reversible token blocking for suspended/deactivated accounts.

    This keeps your access-only JWT flow, but treats tokens as invalid while the
    account is suspended (and valid again immediately after unsuspend).
    """

    def authenticate(self, request):
        out = super().authenticate(request)
        if not out:
            return None
        user, token = out

        # Staff can be suspended too; apply consistently.
        account_status = getattr(user, "account_status", None)
        is_active = getattr(user, "is_active", True)
        if not is_active or account_status in ("SUSPENDED", "DEACTIVATED"):
            raise AuthenticationFailed("Account suspended", code="account_suspended")

        return user, token

