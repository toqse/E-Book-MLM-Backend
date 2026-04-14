from rest_framework.views import exception_handler as drf_exception_handler


def envelope_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    data = response.data
    if isinstance(data, dict) and "success" in data:
        return response
    errors = data
    message = "Validation failed"
    if isinstance(data, dict):
        if "detail" in data and len(data) == 1:
            message = str(data["detail"])
        elif "non_field_errors" in data:
            message = str(data["non_field_errors"][0])
    response.data = {
        "success": False,
        "data": None,
        "message": message,
        "errors": errors,
    }
    return response
