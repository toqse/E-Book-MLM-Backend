from django.db import migrations, models


def backfill_signup_referral_from_otp(apps, schema_editor):
    User = apps.get_model("users", "User")
    OTPRecord = apps.get_model("authentication", "OTPRecord")
    SystemConfig = apps.get_model("admin_panel", "SystemConfig")

    cfg = SystemConfig.objects.order_by("pk").first()
    override = (getattr(cfg, "default_company_referral_code", None) or "").strip()
    from django.conf import settings

    company_code = (
        override
        or (getattr(settings, "DEFAULT_COMPANY_REFERRAL_CODE", "Admin") or "Admin").strip()
    ).upper()

    for user in User.objects.exclude(phone__isnull=True).exclude(phone="").iterator():
        rec = (
            OTPRecord.objects.filter(
                phone=user.phone,
                purpose="REGISTER",
                is_used=True,
            )
            .exclude(registration_referral_code="")
            .order_by("-created_at")
            .first()
        )
        if not rec:
            continue
        signup_code = (rec.registration_referral_code or "").strip()
        if not signup_code:
            continue
        joined = signup_code.upper() == company_code
        User.objects.filter(pk=user.pk).update(
            signup_referral_code=signup_code,
            joined_via_company_referral=joined,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("authentication", "0002_otprecord_registration_fields"),
        ("admin_panel", "0007_systemconfig_default_company_referral_code"),
        ("users", "0010_user_account_status_inactive"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="signup_referral_code",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="user",
            name="joined_via_company_referral",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_signup_referral_from_otp, noop_reverse),
    ]
