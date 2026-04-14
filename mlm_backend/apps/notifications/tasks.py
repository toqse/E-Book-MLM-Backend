from config.celery import app


@app.task
def send_otp_sms_task(phone: str, code: str):
    # Integrate MSG91 / Fast2SMS
    return phone, code


@app.task
def notify_commission_credited(user_id: int, amount: str):
    return user_id, amount
