from __future__ import annotations

import socket

from asyncio.transports import Transport
from typing import TYPE_CHECKING, Callable

from sanic.log import error_logger, logger
from sanic.server.protocols.base_protocol import SanicProtocol


if TYPE_CHECKING:
    from asyncio.events import TimerHandle

    from sanic.app import Sanic

import asyncio

from ssl import SSLContext


# HAProxy PROXY protocol v2 signature (12 octets)
PP2_SIGNATURE = b"\x0d\x0a\x0d\x0a\x00\x0d\x0a\x51\x55\x49\x54\x0a"
PP2_FIXED_HEADER_LEN = 16
PP2_VERSION = 0x2

PP2_CMD_LOCAL = 0x0
PP2_CMD_PROXY = 0x1

PP2_FAM_UNSPEC = 0x0
PP2_FAM_INET = 0x1
PP2_FAM_INET6 = 0x2
PP2_FAM_UNIX = 0x3

PP2_PROTO_UNSPEC = 0x0
PP2_PROTO_STREAM = 0x1
PP2_PROTO_DGRAM = 0x2

# Address block sizes for TCP (STREAM) PROXY commands
_PP2_INET_LEN = 12  # 4 + 4 + 2 + 2
_PP2_INET6_LEN = 36  # 16 + 16 + 2 + 2
_PP2_UNIX_LEN = 216  # 108 + 108

# Sanic only serves TCP and does not negotiate PROXY protocol TLVs, so any
# payload longer than the largest standard address block (UNIX, 216 octets)
# plus a small margin is treated as overlong and rejected.
PP2_MAX_PAYLOAD_LEN = 512


class ProxyProtocolError(ValueError):
    """The received bytes are not a valid PROXY protocol v2 header."""


class ProxyHeader:
    """A validated PROXY protocol v2 header.

    Args:
        command: Either ``PP2_CMD_LOCAL`` or ``PP2_CMD_PROXY``.
        peername: Override transport peername for a PROXY command, or
            ``None`` for a LOCAL command (the underlying address is kept).
        sockname: Override transport sockname for a PROXY command, or
            ``None`` for a LOCAL command.
    """

    __slots__ = ("command", "peername", "sockname")

    def __init__(
        self,
        command: int,
        peername: tuple | None = None,
        sockname: tuple | None = None,
    ):
        self.command = command
        self.peername = peername
        self.sockname = sockname

    @property
    def is_local(self) -> bool:
        return self.command == PP2_CMD_LOCAL


def parse_proxy_header(
    data: bytes | bytearray,
) -> tuple[ProxyHeader | None, int]:
    """Incrementally parse a PROXY protocol v2 header.

    Args:
        data: The bytes received so far on the connection.

    Returns:
        A ``(header, size)`` tuple. ``(None, 0)`` means that a complete
        header has not been received yet and more bytes are required. When a
        valid header is found, ``header`` is a :class:`ProxyHeader` and
        ``size`` is the total number of bytes consumed by the header.

    Raises:
        ProxyProtocolError: The bytes constitute a malformed, overlong or
            incompatible header.
    """
    if len(data) < len(PP2_SIGNATURE):
        return None, 0
    if bytes(data[: len(PP2_SIGNATURE)]) != PP2_SIGNATURE:
        raise ProxyProtocolError("Invalid PROXY protocol v2 signature")

    if len(data) < PP2_FIXED_HEADER_LEN:
        return None, 0

    version_command = data[12]
    if (version_command >> 4) != PP2_VERSION:
        raise ProxyProtocolError(
            f"Unsupported PROXY protocol version: {version_command >> 4}"
        )

    command = version_command & 0x0F
    if command not in (PP2_CMD_LOCAL, PP2_CMD_PROXY):
        raise ProxyProtocolError(
            f"Unsupported PROXY protocol command: {command}"
        )

    family = data[13] >> 4
    transport_protocol = data[13] & 0x0F
    payload_len = int.from_bytes(data[14:16], "big")

    if payload_len > PP2_MAX_PAYLOAD_LEN:
        raise ProxyProtocolError(
            f"PROXY protocol header too long: {payload_len} bytes"
        )

    total_len = PP2_FIXED_HEADER_LEN + payload_len

    # A LOCAL header carries no usable connection information; its payload
    # is skipped. The real socket addresses are retained.
    if command == PP2_CMD_LOCAL:
        if len(data) < total_len:
            return None, 0
        return ProxyHeader(PP2_CMD_LOCAL), total_len

    # Sanic accepts TCP (STREAM) connections only, so datagram and
    # unspecified transport protocols are incompatible.
    if transport_protocol != PP2_PROTO_STREAM:
        raise ProxyProtocolError(
            "PROXY protocol transport protocol does not match the TCP "
            "listener (STREAM is required)"
        )

    if family == PP2_FAM_INET:
        addr_len = _PP2_INET_LEN
    elif family == PP2_FAM_INET6:
        addr_len = _PP2_INET6_LEN
    else:
        # UNSPEC carries no addresses and UNIX cannot describe the peer of
        # an IP socket; both are incompatible with a TCP/IP listener.
        raise ProxyProtocolError(
            "PROXY protocol address family does not match the listener "
            "(IPv4/IPv6 is required)"
        )

    # No TLVs are negotiated, so the declared payload must be exactly the
    # size of the address block. Anything shorter is truncated and anything
    # longer is overlong.
    if payload_len != addr_len:
        raise ProxyProtocolError(
            f"PROXY protocol address data length {payload_len} does not "
            f"match the expected {addr_len} bytes"
        )

    if len(data) < total_len:
        return None, 0

    payload = data[PP2_FIXED_HEADER_LEN:total_len]
    peername: tuple
    sockname: tuple
    if family == PP2_FAM_INET:
        src_ip = socket.inet_ntop(socket.AF_INET, bytes(payload[0:4]))
        dst_ip = socket.inet_ntop(socket.AF_INET, bytes(payload[4:8]))
        src_port = int.from_bytes(payload[8:10], "big")
        dst_port = int.from_bytes(payload[10:12], "big")
        peername = (src_ip, src_port)
        sockname = (dst_ip, dst_port)
    else:
        src_ip = socket.inet_ntop(socket.AF_INET6, bytes(payload[0:16]))
        dst_ip = socket.inet_ntop(socket.AF_INET6, bytes(payload[16:32]))
        src_port = int.from_bytes(payload[32:34], "big")
        dst_port = int.from_bytes(payload[34:36], "big")
        # Match the shape returned by the transport for IPv6 peers so that
        # existing address formatting (brackets etc.) keeps working.
        peername = (src_ip, src_port, 0, 0)
        sockname = (dst_ip, dst_port, 0, 0)

    return (
        ProxyHeader(PP2_CMD_PROXY, peername=peername, sockname=sockname),
        total_len,
    )


def _ssl_protocol_cls(loop: asyncio.AbstractEventLoop):
    """Return the SSLProtocol implementation paired with the event loop."""
    if type(loop).__module__.startswith("uvloop"):
        # uvloop compiles its own API-compatible SSLProtocol into uvloop.loop
        from uvloop.loop import SSLProtocol  # type: ignore[attr-defined]
    else:
        from asyncio.sslproto import SSLProtocol

    return SSLProtocol


def _feed_buffered(protocol, data: bytes) -> None:
    """Replay raw bytes into a buffer-based protocol.

    Both the stdlib and the uvloop SSLProtocol consume bytes through the
    ``get_buffer`` / ``buffer_updated`` buffer protocol API rather than
    ``data_received``.
    """
    view = memoryview(data)
    while view:
        buffer = protocol.get_buffer(len(view))
        if not buffer:
            raise RuntimeError("SSL protocol returned an empty read buffer")
        count = min(len(buffer), len(view))
        buffer[:count] = view[:count]
        protocol.buffer_updated(count)
        view = view[count:]


class ProxyProtocol(asyncio.Protocol):
    """Raw TCP protocol that consumes one PROXY protocol v2 header.

    Every accepted connection buffers bytes until a single, fully validated
    PROXY protocol v2 header has been received. The header is then removed
    from the byte stream and the remaining (and subsequent) bytes are handed
    untouched to the normal HTTP/WebSocket or TLS pipeline.

    A valid PROXY command causes the downstream :class:`ConnInfo` to use the
    source and destination addresses carried by the header. A LOCAL command
    leaves the underlying socket addresses in place. Malformed, overlong,
    incompatible or incomplete headers terminate the connection before any
    request processing takes place.
    """

    __slots__ = (
        "app",
        "loop",
        "transport",
        "connections",
        "inner_factory",
        "ssl_context",
        "_buffer",
        "_delegate",
        "_timeout_handle",
    )

    # Used by the graceful shutdown machinery, which expects this attribute
    # on every tracked connection. It is always falsy at this layer.
    websocket = None

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        app: Sanic,
        connections: set,
        inner_factory: Callable,
        ssl_context: SSLContext | None = None,
    ):
        self.loop = loop
        self.app = app
        self.connections = connections
        self.inner_factory = inner_factory
        self.ssl_context = ssl_context
        self.transport: Transport | None = None
        self._buffer = bytearray()
        self._delegate: asyncio.Protocol | None = None
        self._timeout_handle: TimerHandle | None = None

    # asyncio.Protocol callbacks #
    # --------------------------- #

    def connection_made(self, transport):
        try:
            transport.set_write_buffer_limits(low=16384, high=65536)
            self.transport = transport
            self.connections.add(self)
            # The header must arrive within the same budget as a normal
            # request line, so an idle connection cannot linger forever.
            self._timeout_handle = self.loop.call_later(
                self.app.config.REQUEST_TIMEOUT,
                self._header_timeout,
            )
        except Exception:
            error_logger.exception("proxy_protocol.connection_made")

    def connection_lost(self, exc):
        self._cancel_timeout()
        if self._delegate is not None:
            self._delegate.connection_lost(exc)
            return
        # Closed before the header was handed off. Only this connection is
        # affected; the listening socket and all other connections continue.
        self.connections.discard(self)

    def data_received(self, data: bytes):
        if self._delegate is not None:
            if self.ssl_context is not None:
                # The TLS layer consumes raw bytes via the buffer API.
                _feed_buffered(self._delegate, data)
            else:
                self._delegate.data_received(data)
            return
        self._buffer += data
        self._process_buffer()

    def eof_received(self) -> bool | None:
        if self._delegate is not None:
            return self._delegate.eof_received()
        # Peer shut down writes before completing the header.
        self._reject("Connection closed before PROXY header completed")
        return False

    def pause_writing(self):
        if self._delegate is not None:
            self._delegate.pause_writing()

    def resume_writing(self):
        if self._delegate is not None:
            self._delegate.resume_writing()

    # Connection management used during graceful shutdown #
    # --------------------------------------------------- #

    def close(self, timeout: float | None = None):
        if self.transport is not None and not self.transport.is_closing():
            self.transport.close()

    def abort(self):
        if self.transport is not None:
            self.transport.abort()
            self.transport = None

    def close_if_idle(self) -> bool:
        self.abort()
        return True

    # PROXY header handling #
    # --------------------- #

    def _header_timeout(self):
        self._reject("Timed out waiting for PROXY protocol header")

    def _cancel_timeout(self):
        if self._timeout_handle is not None:
            self._timeout_handle.cancel()
            self._timeout_handle = None

    def _process_buffer(self):
        try:
            header, size = parse_proxy_header(self._buffer)
        except ProxyProtocolError as e:
            self._reject(str(e))
            return
        except Exception as e:  # defensive: never take down the worker
            error_logger.exception("proxy_protocol.parse")
            self._reject(f"Unexpected PROXY header parse error: {e}")
            return

        if header is None:
            # Fragmented header: keep waiting, bounded by the parser.
            return

        remainder = bytes(self._buffer[size:])
        self._buffer.clear()
        self._cancel_timeout()
        self._upgrade(header, remainder)

    def _reject(self, reason: str):
        logger.debug("Rejecting PROXY protocol connection: %s", reason)
        self._cancel_timeout()
        # Drop the connection before it can reach request handling. Errors
        # here are expected when the peer has already gone away.
        try:
            self.abort()
        except Exception:
            error_logger.debug("Failed to close PROXY protocol connection")

    def _upgrade(self, header: ProxyHeader, remainder: bytes):
        # This wrapper leaves the connections set; the inner HTTP protocol
        # adds (and removes) itself through its normal lifecycle callbacks.
        self.connections.discard(self)

        if header.is_local:
            peername_override = None
            sockname_override = None
        else:
            peername_override = header.peername
            sockname_override = header.sockname

        inner: SanicProtocol = self.inner_factory(
            peername_override=peername_override,
            sockname_override=sockname_override,
        )

        try:
            if self.ssl_context is not None:
                # The listening socket is created without ssl= so that the
                # PROXY header can be read in the clear. TLS is now started
                # manually, exactly as create_server(ssl=...) would have.
                # This wrapper stays attached to the raw transport because
                # the TLS layer consumes bytes through the buffer API. The
                # decrypted app transport resolves back to ``inner`` on its
                # own (e.g. for transport.get_protocol() during a WebSocket
                # upgrade).
                ssl_protocol_cls = _ssl_protocol_cls(self.loop)
                delegate = ssl_protocol_cls(
                    self.loop,
                    inner,
                    self.ssl_context,
                    None,
                    server_side=True,
                    server_hostname=None,
                )
                delegate.connection_made(self.transport)
            else:
                delegate = inner
                delegate.connection_made(self.transport)
                # All later lifecycle callbacks (data_received,
                # connection_lost, ...) go straight to the HTTP protocol, so
                # features relying on transport.get_protocol() - notably the
                # WebSocket handshake - keep working.
                set_protocol = getattr(self.transport, "set_protocol", None)
                if set_protocol is not None:
                    set_protocol(delegate)
        except Exception:
            error_logger.exception(
                "Failed to start the downstream protocol for a PROXY "
                "connection"
            )
            # The inner protocol may have registered itself before failing.
            self.connections.discard(inner)
            self.abort()
            return

        self._delegate = delegate

        if remainder:
            # Replay any bytes that arrived in the same segment(s) as the
            # header (e.g. the TLS ClientHello or the HTTP request line).
            try:
                if self.ssl_context is not None:
                    _feed_buffered(delegate, remainder)
                else:
                    delegate.data_received(remainder)
            except Exception:
                error_logger.debug(
                    "Failure while replaying bytes after PROXY header",
                    exc_info=True,
                )
                self._reject("Invalid data following PROXY header")
