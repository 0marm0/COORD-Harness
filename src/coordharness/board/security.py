from __future__ import annotations

import urllib.parse

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _host_name(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("[") and "]" in raw:
        return raw[1:raw.index("]")].lower()
    return raw.split(":", 1)[0].lower()


def host_allowed(host_header: str | None, allowed_hosts=None) -> bool:
    host = _host_name(host_header)
    allowed = {str(h).strip().lower() for h in (allowed_hosts or ALLOWED_HOSTS) if str(h).strip()}
    return bool(host) and host in allowed


def origin_allowed(origin_header: str | None, host_header: str | None) -> bool:
    origin = str(origin_header or "").strip()
    if not origin:
        return True
    try:
        parsed = urllib.parse.urlparse(origin)
    except Exception:
        return False
    if parsed.scheme != "http":
        return False
    return parsed.netloc.lower() == str(host_header or "").strip().lower()


def is_loopback_bind(host: str) -> bool:
    return _host_name(host) in {"127.0.0.1", "localhost", "::1"}


try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse

    class LocalOriginMiddleware(BaseHTTPMiddleware):
        def __init__(self, app, allowed_hosts=None, extra_hosts=()):
            super().__init__(app)
            self.allowed = set(allowed_hosts or ALLOWED_HOSTS) | set(extra_hosts)

        async def dispatch(self, request, call_next):
            host_header = request.headers.get("host", "")
            if not host_allowed(host_header, self.allowed):
                return PlainTextResponse("forbidden host", status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if not origin_allowed(request.headers.get("origin", ""), host_header):
                    return PlainTextResponse("forbidden origin", status_code=403)
            return await call_next(request)

except ImportError:
    LocalOriginMiddleware = None
