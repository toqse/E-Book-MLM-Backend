from rest_framework.exceptions import Throttled
from rest_framework.views import exception_handler as drf_exception_handler


def _first_validation_message(data) -> str:
    """Surface the first human-readable validation line as the top-level message."""
    if not isinstance(data, dict):
        return "Validation failed"
    if "detail" in data and len(data) == 1:
        d = data["detail"]
        if isinstance(d, list) and d:
            return str(d[0])
        return str(d)
    if "non_field_errors" in data:
        nfe = data["non_field_errors"]
        if isinstance(nfe, list) and nfe:
            return str(nfe[0])
        if isinstance(nfe, str):
            return nfe
    for key, val in data.items():
        if key in ("detail", "non_field_errors"):
            continue
        if isinstance(val, list) and val:
            return str(val[0])
        if isinstance(val, dict):
            nested = _first_validation_message(val)
            if nested != "Validation failed":
                return nested
        if isinstance(val, str):
            return val
    return "Validation failed"


def envelope_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    data = response.data
    if isinstance(data, dict) and "success" in data:
        return response

    if isinstance(exc, Throttled):
        wait = int(exc.wait) if exc.wait else None
        response.data = {
            "success": False,
            "data": None,
            "message": "OTP limit exceeded. Try again later",
            "errors": {"detail": "rate_limited", "retry_after_seconds": wait},
        }
        return response

    errors = data
    message = _first_validation_message(data)
    response.data = {
        "success": False,
        "data": None,
        "message": message,
        "errors": errors,
    }
    return response
