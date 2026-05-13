import pytest

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
