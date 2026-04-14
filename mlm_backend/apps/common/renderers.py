from rest_framework.renderers import JSONRenderer


class EnvelopeJSONRenderer(JSONRenderer):
    """Wraps DRF response in {success, data, message, errors} when not already wrapped."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        renderer_context = renderer_context or {}
        response = renderer_context.get("response")
        request = renderer_context.get("request")

        if response is not None and getattr(request, "_envelope_skip", False):
            return super().render(data, accepted_media_type, renderer_context)

        if isinstance(data, dict) and "success" in data:
            return super().render(data, accepted_media_type, renderer_context)

        success = True
        message = "OK"
        errors = None
        payload = data

        if response is not None and response.status_code >= 400:
            success = False
            message = "Request failed"
            errors = data if isinstance(data, dict) else {"detail": data}
            payload = None
        elif response is not None and 200 <= response.status_code < 300:
            if isinstance(data, dict) and "detail" in data and len(data) == 1:
                message = str(data["detail"])
                payload = None
            else:
                payload = data

        wrapped = {"success": success, "data": payload, "message": message, "errors": errors}
        return super().render(wrapped, accepted_media_type, renderer_context)
