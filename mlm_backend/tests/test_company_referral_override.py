import pytest
from rest_framework.test import APIClient

from apps.admin_panel.utils import get_system_config
from apps.users.services import effective_company_referral_code, environment_company_referral_code


@pytest.mark.django_db
def test_effective_company_referral_uses_system_config_override(settings):
    settings.DEFAULT_COMPANY_REFERRAL_CODE = "FromEnv"
    cfg = get_system_config()
    cfg.default_company_referral_code = "DbOverride"
    cfg.save()
    assert effective_company_referral_code() == "DbOverride"
    assert environment_company_referral_code() == "FromEnv"


@pytest.mark.django_db
def test_effective_company_referral_falls_back_to_env_when_override_blank(settings):
    settings.DEFAULT_COMPANY_REFERRAL_CODE = "EnvOnly"
    cfg = get_system_config()
    cfg.default_company_referral_code = ""
    cfg.save()
    assert effective_company_referral_code() == "EnvOnly"


@pytest.mark.django_db
def test_public_company_referral_code_endpoint_no_auth(settings):
    settings.DEFAULT_COMPANY_REFERRAL_CODE = "PublicEnv"
    cfg = get_system_config()
    cfg.default_company_referral_code = "PublicOverride"
    cfg.save()

    client = APIClient()
    resp = client.get("/api/v1/auth/company-referral-code/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["default_company_referral_code"] == "PublicOverride"


@pytest.mark.django_db
def test_public_company_referral_code_endpoint_falls_back_to_env(settings):
    settings.DEFAULT_COMPANY_REFERRAL_CODE = "EnvFallback"
    cfg = get_system_config()
    cfg.default_company_referral_code = ""
    cfg.save()

    client = APIClient()
    resp = client.get("/api/v1/auth/company-referral-code/")
    assert resp.status_code == 200
    assert resp.json()["data"]["default_company_referral_code"] == "EnvFallback"
