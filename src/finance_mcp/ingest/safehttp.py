"""safehttp.py — SSRF-resistant outbound HTTP (public-service security posture).

Every outbound request in the suite goes through fetch(): it enforces HTTPS (the
only exception is explicitly-opted-in localhost, for the local bank-mcp server),
optionally pins an allowlist of hosts, always bounds the request with a timeout,
and refuses redirects to non-HTTPS targets — so a future edit that lets a URL come
from config/user input can't be turned into an SSRF or a plaintext downgrade.

Centralizing this replaces the per-call-site `startswith("https://")` guards that
were duplicated (and missing) across the codebase.
"""

import ssl
import urllib.request
import urllib.error
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 30


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to HTTPS targets; block plaintext/scheme downgrades."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(
                req.full_url, code, f"blocked non-HTTPS redirect to {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _verified_context():
    """A TLS context that ALWAYS verifies certs. Prefer certifi's bundle (some
    Python builds — e.g. python.org macOS framework — ship with no system CA file,
    which would otherwise make every HTTPS call fail). Never disable verification."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_OPENER = urllib.request.build_opener(
    _SafeRedirect,
    urllib.request.HTTPSHandler(context=_verified_context()),
)


def _validate(url, allow_localhost, allowed_hosts):
    p = urlparse(url)
    is_https = p.scheme == "https"
    is_local = (allow_localhost and p.scheme == "http"
                and p.hostname in ("localhost", "127.0.0.1"))
    if not (is_https or is_local):
        raise ValueError(f"Refusing non-HTTPS outbound URL: {url}")
    if allowed_hosts is not None and p.hostname not in allowed_hosts:
        raise ValueError(f"Host not allowlisted ({p.hostname}): {url}")


def fetch(url, *, data=None, headers=None, method=None, timeout=DEFAULT_TIMEOUT,
          allow_localhost=False, allowed_hosts=None):
    """Open a URL or a urllib Request safely. Returns the response object (use as a
    context manager). Raises ValueError if the scheme/host is not allowed.

    `allow_localhost` permits http://localhost|127.0.0.1 (the local bank-mcp server).
    `allowed_hosts` (iterable) pins the request to a set of hostnames.
    """
    if isinstance(url, urllib.request.Request):
        req = url
    else:
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    # Validate the Request's final URL (covers both string and Request inputs).
    _validate(req.full_url, allow_localhost, allowed_hosts)
    return _OPENER.open(req, timeout=timeout)
