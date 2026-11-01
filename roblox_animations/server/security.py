"""
Request validation for the localhost sync servers.

Studio's HttpService and WebStreamClient never send browser Origin headers, so
anything that does is a web page trying to reach Blender through the user's
browser. Host checks stop DNS-rebinding pages that resolve their own domain to
127.0.0.1. Together they keep the sync servers usable from Studio (and other
local tools) while closing them to the web, with no token for the user to copy.
"""

from __future__ import annotations

import urllib.parse
import zlib

# Studio animation payloads are JSON; a long, many-bone animation is tens of MB
# at most. These caps only stop runaway or hostile inputs.
MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_TRUSTED_ORIGIN_SUFFIXES = (".roblox.com",)
_TRUSTED_ORIGIN_HOSTS = frozenset({"roblox.com"})
_BROWSER_ONLY_SCHEMES = frozenset({
    "file", "chrome-extension", "moz-extension", "safari-web-extension", "edge-extension",
})


class RequestRejected(Exception):
    """A request failed validation; ``status`` is the HTTP code to answer."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def host_allowed(host_header) -> bool:
    """True when the Host header names this machine's loopback interface."""
    if not host_header:
        # HTTP/1.0 clients may omit Host; browsers never do.
        return True
    host = str(host_header).strip().lower()
    if host.startswith("["):
        end = host.find("]")
        name = host[: end + 1] if end >= 0 else host
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in _LOOPBACK_HOSTS


def origin_allowed(origin) -> bool:
    """True for requests that did not come from a web page.

    Browsers attach Origin to every cross-origin fetch, form POST and WebSocket
    handshake. Studio does not. Roblox's own domains are trusted in case a
    Roblox client ever starts sending one.
    """
    if origin is None:
        return True
    origin = str(origin).strip()
    if not origin:
        return True
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    if origin.lower() == "null" or scheme in _BROWSER_ONLY_SCHEMES:
        # Opaque origins (file:// pages, sandboxed frames) and extensions.
        return False
    if scheme not in {"http", "https"}:
        # Browsers only send http(s), "null" or extension origins; anything
        # else comes from a non-browser client.
        return True
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host in _TRUSTED_ORIGIN_HOSTS or host.endswith(_TRUSTED_ORIGIN_SUFFIXES)


def validate_request_headers(headers) -> None:
    """Raise RequestRejected for requests that did not come from Studio."""
    if not host_allowed(headers.get("Host")):
        raise RequestRejected(403, "Requests must target localhost.")
    if not origin_allowed(headers.get("Origin")):
        raise RequestRejected(
            403,
            "Blocked a request from a web page. Only Roblox Studio can talk to this server.",
        )


def read_request_body(headers, rfile, limit: int = MAX_REQUEST_BODY_BYTES) -> bytes:
    """Read the request body, enforcing Content-Length and the size cap."""
    raw_length = headers.get("Content-Length")
    if raw_length in (None, ""):
        return b""
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        raise RequestRejected(400, "Invalid Content-Length header.") from None
    if length < 0:
        raise RequestRejected(400, "Invalid Content-Length header.")
    if length > limit:
        raise RequestRejected(
            413, f"Request body is {length} bytes; the limit is {limit} bytes."
        )
    data = rfile.read(length) if length else b""
    if len(data) != length:
        raise RequestRejected(400, "Request body ended early.")
    return data


def decompress_body(data: bytes, limit: int = MAX_DECOMPRESSED_BYTES) -> bytes:
    """Undo gzip/zlib compression with a hard output cap; plain data passes through."""
    if len(data) < 2:
        return data
    if data[:2] == b"\x1f\x8b":
        wbits = 16 + zlib.MAX_WBITS
    elif data[0] == 0x78 and (data[0] * 256 + data[1]) % 31 == 0:
        wbits = zlib.MAX_WBITS
    else:
        return data
    decompressor = zlib.decompressobj(wbits)
    try:
        output = decompressor.decompress(data, limit + 1)
    except zlib.error:
        # Not actually compressed (a JSON body that happens to start with 'x').
        return data
    if len(output) > limit or decompressor.unconsumed_tail:
        raise RequestRejected(413, f"Decompressed body exceeds {limit} bytes.")
    return output


def is_json_or_binary_content_type(content_type) -> bool:
    """POSTs that change Blender's scene must use a non-CORS-safelisted type.

    Browsers can only send text/plain, form or multipart bodies without a
    preflight; Studio sends application/json or application/octet-stream.
    """
    if not content_type:
        return False
    base = str(content_type).split(";", 1)[0].strip().lower()
    return base in {"application/json", "application/octet-stream"}
