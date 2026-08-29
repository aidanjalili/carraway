"""HTTP for the sync providers, with a working address family.

Some providers publish AAAA records that do not answer. Python resolves a
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

# A single connect attempt. Kept small because several are tried in a row.
CONNECT_TIMEOUT = 5.0


def connect_ipv4(host: str, port: int, timeout=None, source_address=None) -> socket.socket:
    """Open a TCP connection over IPv4 only.

    socket.create_connection has no family argument, so addresses are resolved
    here and only A records are tried. Raises the last error if all of them
    fail, matching what create_connection does across families.
    """
    last: Exception | None = None
    for family, kind, proto, _, address in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        sock = socket.socket(family, kind, proto)
        try:
            # Cap each attempt: a host with several A records should not
            # multiply one slow address by the number of addresses.
            sock.settimeout(min(timeout, CONNECT_TIMEOUT) if timeout else CONNECT_TIMEOUT)
            if source_address:
                sock.bind(source_address)
            sock.connect(address)
            # Restore the caller's timeout for the exchange itself; the short
            # one above is only about how long to wait for a connection.
            sock.settimeout(timeout)
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
    """Open a request, trying IPv4 before falling back to normal resolution.

    IPv4 first, which is the opposite of what Python does by default. A host
    with dead AAAA records costs more than it looks: `getaddrinfo` can return
    a dozen IPv6 addresses, and `create_connection` tries each one with the
    full timeout in turn. At even a modest per-attempt timeout that is a
    minute of apparent hang before anything else is tried, which reads to a
    waiting user as a broken application.

    The default path is still the fallback, so an IPv6-only network keeps
    working: it is tried second rather than not at all.
    """
    try:
        return _ipv4_opener.open(request, timeout=timeout)
    except urllib.error.HTTPError:
        # A real answer from the server. Retrying over another address family
        # would just ask the same question again and get the same reply.
        raise
    except (TimeoutError, urllib.error.URLError, OSError):
        return urllib.request.urlopen(request, timeout=timeout)
