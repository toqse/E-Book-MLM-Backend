import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_send_otp_rate_limit():
    client = APIClient()
    for _ in range(3):
        r = client.post(
            "/api/v1/auth/send-otp/",
            {"phone": "9000000001", "purpose": "LOGIN"},
            format="json",
        )
        assert r.status_code == 200
    r = client.post(
        "/api/v1/auth/send-otp/",
        {"phone": "9000000001", "purpose": "LOGIN"},
        format="json",
    )
    assert r.status_code == 429
