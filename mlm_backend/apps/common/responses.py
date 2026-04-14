from rest_framework.response import Response


def envelope_response(data=None, message="Operation successful", success=True, errors=None, status=200):
    return Response(
        {"success": success, "data": data, "message": message, "errors": errors},
        status=status,
    )
