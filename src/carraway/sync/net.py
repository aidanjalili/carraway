"""HTTP for the sync providers, with a working address family.

`api.venmo.com` publishes AAAA records that do not answer. Python resolves a
host and prefers IPv6, so every request hangs until it times out on a machine
whose IPv6 is otherwise fine — which reads to the user as "the app is broken"
rather than "one endpoint's IPv6 is broken".

Browsers solved this years ago with Happy Eyeballs (RFC 8305): try both
families and use whichever answers. That is more machinery than this needs, so
the approach here is narrower — make the request normally, and on a connection
timeout retry pinned to IPv4. A host with working IPv6 is unaffected; a host
with broken IPv6 costs one timeout and then works.
"""

from __future__ import annotations

import http.client
import socket
import urllib.error
import urllib.request

# Long enough not to trip on a slow-but-working IPv6 path, short enough that a
# dead one does not look like a hang.
FIRST_ATTEMPT_TIMEOUT = 8.0


def connect_ipv4(host: str, port: int, timeout=None, source_address=None) -> socket.socket:
    """Open a TCP connection over IPv4 only.

    Raises the last error if every A record fails, matching what
    socket.create_connection does across families.
    """
    last: Exception | None = None
    for family, kind, proto, _, address in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(address)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last or OSError(f"no IPv4 address for {host}")


class _IPv4Connection(http.client.HTTPSConnection):
    """An HTTPS connection pinned to IPv4.

    The hostname is still what TLS verifies against, so certificate checking
    is unaffected — only address selection changes.
    """

    def connect(self) -> None:
        # socket.create_connection has no family argument, so the address is
        # resolved here and only AF_INET results are tried.
        self.sock = connect_ipv4(self.host, self.port, self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _IPv4Handler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4Connection, req)


_ipv4_opener = urllib.request.build_opener(_IPv4Handler)


def urlopen(request, timeout: float = 30.0):
    """Open a request, falling back to IPv4 if the first attempt times out.

    Only a timeout triggers the retry. An HTTP error is a real answer from the
    server and must propagate untouched, or a 403 would be retried as though
    it were a network fault.
    """
    first = min(timeout, FIRST_ATTEMPT_TIMEOUT)
    try:
        return urllib.request.urlopen(request, timeout=first)
    except urllib.error.HTTPError:
        raise
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if not isinstance(reason, TimeoutError | socket.timeout | OSError):
            raise
        return _ipv4_opener.open(request, timeout=timeout)
