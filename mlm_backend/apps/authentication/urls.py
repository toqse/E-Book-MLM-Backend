from django.urls import path

from . import views

urlpatterns = [
    path("send-otp/", views.send_otp),
    path("register/send-otp/", views.send_register_otp),
    path("verify-otp-register/", views.verify_otp_register),
    path("verify-otp-login/", views.verify_otp_login),
    path("logout/", views.logout),
    path("me/", views.me),
    path("kyc/submit/", views.kyc_submit),
    path("kyc/status/", views.kyc_status),
    path("bank/", views.bank_update),
    path("validate-referral/", views.validate_referral),
]
