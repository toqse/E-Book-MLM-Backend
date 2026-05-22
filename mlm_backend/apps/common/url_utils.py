"""Helpers for building public-facing absolute URLs that survive HTTPS proxies.

When the backend sits behind a reverse proxy / tunnel (cloudflared, nginx, Azure
Front Door, etc.) the request between the proxy and Django is plain HTTP. If
``SECURE_PROXY_SSL_HEADER`` / ``X-Forwarded-Proto`` are not honoured for any
reason (settings module loaded earlier than expected, proxy not forwarding the
header, hostname rewrites, …) ``request.build_absolute_uri()`` falls back to
``http://<public-host>/...`` which the browser then blocks as mixed content
when the SPA is served over HTTPS (Azure Static Web Apps in our case).

These helpers normalize that to ``https://`` for any non-local public host and
let ops override the host entirely via ``PUBLIC_BACKEND_BASE_URL``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from django.conf import settings

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def public_absolute_uri(request: Any, path_or_url: str | None) -> str | None:
    """Return an HTTPS-safe absolute URL for ``path_or_url``.

    Resolution order:
      1. If ``PUBLIC_BACKEND_BASE_URL`` is set, join the relative path to it.
      2. Otherwise use ``request.build_absolute_uri()``.
      3. If the result is ``http://`` and the host is not a loopback address,
         rewrite the scheme to ``https://`` so the browser doesn't block it.

    Already-absolute URLs are returned through the same scheme-normalization.
    """
    if path_or_url in (None, ""):
        return None

    base = (getattr(settings, "PUBLIC_BACKEND_BASE_URL", "") or "").strip().rstrip("/")
    if base and not str(path_or_url).startswith(("http://", "https://")):
        return urljoin(f"{base}/", str(path_or_url).lstrip("/"))

    if request is None:
        return _force_https_if_public(str(path_or_url))

    try:
        absolute = request.build_absolute_uri(path_or_url)
    except Exception:
        return str(path_or_url)
    return _force_https_if_public(absolute)


def public_media_url(request: Any, file_field: Any) -> str | None:
    """Convenience wrapper for ``FieldFile`` (returns ``None`` if missing/empty)."""
    if not file_field:
        return None
    name = (getattr(file_field, "name", "") or "").strip()
    if not name:
        return None
    try:
        rel_url = file_field.url
    except Exception:
        return None
    return public_absolute_uri(request, rel_url)


def _force_https_if_public(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "http":
        return url
    hostname = (parts.hostname or "").lower()
    if not hostname or hostname in _LOCAL_HOSTS:
        return url
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
