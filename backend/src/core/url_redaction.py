"""Make connection URLs safe to log.

Broker, cache and database URLs often carry a password
(``redis://:secret@host:6379/0``). Log only what identifies the endpoint:
scheme, host and port. Path, query and userinfo are dropped, because a DB
index or query parameter can also hold a token.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

_UNPARSEABLE = "<unparseable-url>"


def redact_url(url: Optional[str]) -> str:
    """Return ``scheme://host[:port]`` for ``url``, never its credentials.

    Anything that does not parse as ``scheme://host`` returns a fixed
    placeholder instead of echoing the input, which might be a bare secret.
    """
    if not url:
        return "<unset>"
    try:
        parts = urlsplit(str(url))
        host = parts.hostname
        port = parts.port
    except ValueError:
        return _UNPARSEABLE
    if not parts.scheme or not host:
        return _UNPARSEABLE
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")
