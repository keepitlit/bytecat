"""The local-only guard shared by every HTTP client in bytecat.

The hard requirement behind this whole project is that personal file content
never leaves the machine. llm.py (the model server) and chroma.py (vectors,
which are derived from file content) both send every request through `opener`,
after checking the base URL with `assert_local`. There is no configuration
option that relaxes it.
"""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


class LocalOnlyError(RuntimeError):
    """Raised when something tries to point the pipeline off-box."""


def assert_local(url: str, what: str) -> None:
    """Refuse `url` unless it names a loopback host. `what` names the endpoint."""
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK:
        # A hard invariant, not a default: no setting relaxes it.
        raise LocalOnlyError(f"refusing non-loopback {what} endpoint {host!r}")


class _LocalOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Re-apply the guard to every redirect hop.

    urllib follows 301/302/303 by itself without consulting us, so checking only
    the base URL leaves a gap: a process holding the model port could bounce the
    request off-box. Python drops the request body on those codes (and refuses
    307/308 outright for a POST), so file content cannot ride along -- but the
    request itself would still leave, which is enough to beacon. Refuse instead.
    """

    def __init__(self, what: str):
        super().__init__()
        self.what = what

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_local(newurl, self.what)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener(what: str) -> urllib.request.OpenerDirector:
    """An opener whose every redirect hop is checked. Callers send every request
    through one of these, never through the module-level urlopen, which would
    use the default redirect handler."""
    return urllib.request.build_opener(_LocalOnlyRedirects(what))
