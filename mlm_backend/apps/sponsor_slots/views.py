from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.common.permissions import IsAdminRole
from apps.common.responses import envelope_response

from .models import SponsorSlotBatch, SponsorSlotCode
from .services import SponsorSlotService


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_slots(request):
    batches = SponsorSlotBatch.objects.filter(issued_to=request.user).prefetch_related("codes")
    data = []
    for b in batches:
        data.append(
            {
                "batch_id": b.id,
                "band": b.band_number,
                "expires_at": b.expires_at.isoformat(),
                "codes": [
                    {
                        "code": c.code,
                        "status": c.status,
                        "expires_at": c.expires_at.isoformat(),
                        "unlock_at_total_earned": str(c.unlock_at_total_earned)
                        if c.unlock_at_total_earned is not None
                        else None,
                    }
                    for c in b.codes.all()
                ],
            }
        )
    return envelope_response({"batches": data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def share_code(request, code: str):
    c = SponsorSlotCode.objects.filter(code__iexact=code, issued_to=request.user).first()
    if not c:
        return envelope_response(None, message="Not found", success=False, status=404)
    c.shared_via = request.data.get("channel", "COPY")
    c.status = SponsorSlotCode.Status.SHARED
    c.save(update_fields=["shared_via", "status"])
    return envelope_response({"ok": True})


@api_view(["POST"])
@permission_classes([AllowAny])
def validate_public(request):
    code = request.data.get("code") or request.data.get("sponsor_code")
    slot = SponsorSlotService.validate_code(code or "")
    if not slot:
        return envelope_response(None, message="Invalid or expired", success=False, status=400)
    return envelope_response({"valid": True, "issuer": slot.issued_to.full_name})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_slots(request):
    qs = SponsorSlotBatch.objects.all()[:100]
    return envelope_response({"count": qs.count()})


@api_view(["GET"])
@permission_classes([IsAdminRole])
def admin_slots_flagged(request):
    qs = SponsorSlotCode.objects.filter(is_flagged=True)
    return envelope_response({"results": [c.code for c in qs]})


@api_view(["POST"])
@permission_classes([IsAdminRole])
def admin_expire_code(request, code: str):
    c = SponsorSlotCode.objects.filter(code__iexact=code).first()
    if not c:
        return envelope_response(None, message="Not found", success=False, status=404)
    c.status = SponsorSlotCode.Status.EXPIRED
    c.save(update_fields=["status"])
    return envelope_response({"ok": True})
