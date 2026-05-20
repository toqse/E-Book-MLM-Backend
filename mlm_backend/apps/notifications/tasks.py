from config.celery import app


@app.task
def send_otp_sms_task(phone: str, code: str):
    # Integrate MSG91 / Fast2SMS
    return phone, code


@app.task
def notify_commission_credited(user_id: int, amount: str):
    return user_id, amount


@app.task
def send_kyc_invitation_sms_task(phone: str, link: str):
    # Integrate MSG91 when SMS_PROVIDER_API_KEY and template id are configured.
    return phone, link
