"""Read-only HTTP access to a running ComfyUI server."""
import http.client
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:8188"


class TransientError(RuntimeError):
    """The server could not be reached right now; trying again may work."""


def valid_host(value):
    host = (value or "").strip().rstrip("/") or DEFAULT_URL
    parsed = urlparse(host)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"Enter a valid ComfyUI URL, for example {DEFAULT_URL}.")
    return host


def get_json(host, route, timeout=5):
    """GET a ComfyUI route and return its parsed JSON (None for an empty reply)."""
    try:
        with urllib.request.urlopen(host.rstrip("/") + route, timeout=timeout) as reply:
            payload = reply.read()
    except urllib.error.HTTPError as error:
        problem = TransientError if error.code in (408, 429, 500, 502, 503, 504) else RuntimeError
        raise problem(f"ComfyUI answered HTTP {error.code} on {route}.") from error
    except urllib.error.URLError as error:
        raise TransientError(f"Could not reach ComfyUI at {host}: {error.reason}") from error
    except (OSError, http.client.HTTPException) as error:
        raise TransientError(f"Could not reach ComfyUI at {host}: {error}") from error
    try:
        return json.loads(payload) if payload.strip() else None
    except ValueError as error:
        raise TransientError(f"ComfyUI sent an unreadable reply on {route}.") from error
