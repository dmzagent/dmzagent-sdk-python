"""The SDK's HTTP layer, on the standard library.

The SDK used to depend on httpx for one thing: send a JSON request, read a
JSON reply. `urllib.request` does that, so the package now installs with no
third-party dependencies at all. A process that imports it, such as an agent
runner holding a model key, a memory key and a push credential, gains no
code its owners did not write.

The public `transport=` argument keeps working. An httpx transport passed by
an existing caller, a test's `httpx.MockTransport` included, is adapted
here. httpx is imported only when one is passed, so it is needed only by
callers who were already using it.

What goes over the wire is unchanged. JSON bodies are encoded exactly the way
httpx 0.28 encodes them (compact separators, UTF-8, no NaN), so a body and its
Content-Length are byte-for-byte what they were.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol, cast


class TransportError(Exception):
    """The request did not complete: no connection, a reset, a bad URL."""


class TransportTimeout(TransportError):
    """The request did not complete in time."""


class Headers:
    """Case-insensitive, read-only response headers."""

    def __init__(self, items: Mapping[str, str] | list[tuple[str, str]] | None = None) -> None:
        pairs = list(items.items()) if isinstance(items, Mapping) else list(items or [])
        self._d = {k.lower(): v for k, v in pairs}

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._d.get(name.lower(), default)

    def __getitem__(self, name: str) -> str:
        return self._d[name.lower()]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.lower() in self._d


class Response:
    """A completed HTTP exchange: status, headers and body bytes."""

    def __init__(self, status_code: int, headers: Headers | None = None,
                 content: bytes = b"") -> None:
        self.status_code = status_code
        self.headers = headers or Headers()
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


class Transport(Protocol):
    """Sends one request. The default is `UrllibTransport`."""

    def send(self, method: str, url: str, headers: dict[str, str],
             body: bytes | None, timeout: float) -> Response: ...

    def close(self) -> None: ...


def encode_json(value: Any) -> bytes:
    """The bytes httpx 0.28 sends for `json=value`."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


class UrllibTransport:
    """The default transport. One request per call, nothing pooled."""

    def send(self, method: str, url: str, headers: dict[str, str],
             body: bytes | None, timeout: float) -> Response:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, Headers(resp.getheaders()), resp.read())
        except urllib.error.HTTPError as e:
            # A non-2xx reply is a reply. The client maps statuses to errors,
            # so the transport must not turn one into an exception.
            with e:
                return Response(e.code, Headers(e.headers.items() if e.headers else []),
                                e.read())
        except TimeoutError as e:
            raise TransportTimeout(str(e)) from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TransportTimeout(str(e.reason)) from e
            raise TransportError(str(e.reason)) from e
        except OSError as e:
            raise TransportError(str(e)) from e

    def close(self) -> None:
        return None


class HttpxTransportAdapter:
    """Runs an httpx transport the caller passed, through this layer.

    httpx is imported here and nowhere else, and only when such a transport
    exists. Its two exception families are translated to this module's, so
    the client's error mapping is the same whichever transport is in use.
    """

    def __init__(self, transport: Any) -> None:
        import httpx

        self._httpx = httpx
        self._transport = transport

    def send(self, method: str, url: str, headers: dict[str, str],
             body: bytes | None, timeout: float) -> Response:
        httpx = self._httpx
        request = httpx.Request(method, url, headers=headers, content=body,
                                extensions={"timeout": httpx.Timeout(timeout).as_dict()})
        try:
            resp = self._transport.handle_request(request)
            content = resp.read()
        except httpx.TimeoutException as e:
            raise TransportTimeout(str(e)) from e
        except httpx.RequestError as e:
            raise TransportError(str(e)) from e
        return Response(resp.status_code, Headers(list(resp.headers.items())), content)

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()


def resolve_transport(transport: Any) -> Transport:
    """The caller's transport if it speaks this layer, else an adapted httpx
    one, else the standard-library default."""
    if transport is None:
        return UrllibTransport()
    if callable(getattr(transport, "send", None)) and not hasattr(transport, "handle_request"):
        return cast(Transport, transport)
    if callable(getattr(transport, "handle_request", None)):
        return HttpxTransportAdapter(transport)
    raise TypeError(
        f"transport must be a dmzagent transport (with send) or an httpx transport "
        f"(with handle_request), not {type(transport).__name__}")


class HTTPClient:
    """What the clients need from HTTP: GET, PUT and POST, with JSON bodies."""

    def __init__(self, *, headers: dict[str, str], timeout: float,
                 transport: Any = None, base_url: str = "") -> None:
        self.headers = dict(headers)
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._transport = resolve_transport(transport)

    def _url(self, url: str, params: Mapping[str, str] | None) -> str:
        full = f"{self.base_url}{url}" if url.startswith("/") and self.base_url else url
        if params:
            sep = "&" if urllib.parse.urlsplit(full).query else "?"
            full = f"{full}{sep}{urllib.parse.urlencode(list(params.items()))}"
        return full

    def request(self, method: str, url: str, *, params: Mapping[str, str] | None = None,
                json_body: Any = None, has_body: bool = False,
                headers: Mapping[str, str] | None = None) -> Response:
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        body = encode_json(json_body) if has_body else None
        if body is not None:
            merged["Content-Length"] = str(len(body))
        return self._transport.send(method, self._url(url, params), merged, body,
                                    self.timeout)

    def get(self, url: str, params: Mapping[str, str] | None = None) -> Response:
        return self.request("GET", url, params=params)

    def put(self, url: str, json: Any) -> Response:
        return self.request("PUT", url, json_body=json, has_body=True)

    def post(self, url: str, json: Any, headers: Mapping[str, str] | None = None) -> Response:
        return self.request("POST", url, json_body=json, has_body=True, headers=headers)

    def close(self) -> None:
        self._transport.close()
