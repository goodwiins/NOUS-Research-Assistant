"""shared.utils must not offer an unguarded fetch-any-URL helper (GOO-587).

``make_http_request`` fetched whatever URL it was given, with no SSRF guard,
so the first caller to pass user input would have been an SSRF. It had no
callers and was removed. Outbound calls to user-supplied URLs go through
``src.utils.outbound_url_guard``.
"""

from src.shared import utils


def test_make_http_request_is_gone() -> None:
    assert not hasattr(utils, "make_http_request")
