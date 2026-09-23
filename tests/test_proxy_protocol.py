import asyncio
import json
import os
import socket
import ssl
import struct
import sys
import tempfile

import pytest

from sanic import text
from sanic.compat import use_context
from sanic.exceptions import ServerError
from sanic.http.tls.context import CertSimple
from sanic.response import json as sanic_json
from sanic.server.protocols.http_protocol import HttpProtocol
from sanic.server.protocols.proxy_protocol import (
    PP2_FIXED_HEADER_LEN,
    PP2_SIGNATURE,
    ProxyProtocol,
    ProxyProtocolError,
    parse_proxy_header,
)


CERT_DIR = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "certs", "localhost"
)
CERT_FILE = os.path.join(CERT_DIR, "fullchain.pem")
KEY_FILE = os.path.join(CERT_DIR, "privkey.pem")

HOST = "127.0.0.1"
HTTP_REQUEST = (
    b"GET / HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n"
)


def pp2_header(
    src_ip,
    dst_ip,
    src_port=55555,
    dst_port=80,
    *,
    command=0x1,
    family=None,
    protocol=0x1,
    payload: bytes | None = None,
):
    """Build a PROXY protocol v2 header."""
    if family is None:
        family = 0x2 if ":" in src_ip else 0x1
    if payload is None:
        af = socket.AF_INET6 if family == 0x2 else socket.AF_INET
        payload = (
            socket.inet_pton(af, src_ip)
            + socket.inet_pton(af, dst_ip)
            + struct.pack("!HH", src_port, dst_port)
        )
    return (
        PP2_SIGNATURE
        + bytes([0x20 | command, (family << 4) | protocol])
        + struct.pack("!H", len(payload))
        + payload
    )


def pp2_local(payload: bytes = b""):
    return (
        PP2_SIGNATURE
        + bytes([0x20, 0x00])
        + struct.pack("!H", len(payload))
        + payload
    )


# -------------------------------------------------------------------------- #
# Parser unit tests
# -------------------------------------------------------------------------- #


def test_parse_valid_ipv4():
    header = pp2_header("1.2.3.4", "5.6.7.8", 12345, 8080)
    parsed, size = parse_proxy_header(header)
    assert size == 28
    assert parsed.command == 0x1
    assert parsed.peername == ("1.2.3.4", 12345)
    assert parsed.sockname == ("5.6.7.8", 8080)
    assert not parsed.is_local


def test_parse_valid_ipv6():
    header = pp2_header("2001:db8::1", "2001:db8::2", 1111, 2222)
    parsed, size = parse_proxy_header(header)
    assert size == 52
    assert parsed.peername == ("2001:db8::1", 1111, 0, 0)
    assert parsed.sockname == ("2001:db8::2", 2222, 0, 0)


def test_parse_local():
    parsed, size = parse_proxy_header(pp2_local())
    assert size == PP2_FIXED_HEADER_LEN
    assert parsed.is_local
    assert parsed.peername is None
    assert parsed.sockname is None


def test_parse_local_with_ignored_payload():
    parsed, size = parse_proxy_header(pp2_local(b"\x00" * 10))
    assert parsed.is_local
    assert size == 26


@pytest.mark.parametrize("cut", range(1, 28))
def test_parse_fragmented_ipv4_waits_for_completion(cut):
    header = pp2_header("1.2.3.4", "5.6.7.8")
    parsed, size = parse_proxy_header(header[:cut])
    assert parsed is None
    assert size == 0


def test_parse_fragmented_ipv6_waits_for_completion():
    header = pp2_header("2001:db8::1", "2001:db8::2")
    for cut in range(1, len(header)):
        parsed, _ = parse_proxy_header(header[:cut])
        assert parsed is None
    parsed, size = parse_proxy_header(header)
    assert parsed.peername[0] == "2001:db8::1"
    assert size == 52


def test_parse_ignores_trailing_bytes():
    header = pp2_header("1.2.3.4", "5.6.7.8")
    parsed, size = parse_proxy_header(header + b"GET / HTTP/1.1")
    assert parsed.peername == ("1.2.3.4", 55555)
    assert size == 28


def test_parse_rejects_bad_signature():
    with pytest.raises(ProxyProtocolError):
        parse_proxy_header(b"not a proxy header" + b"\x00" * 20)


def test_parse_rejects_bad_version():
    data = PP2_SIGNATURE + bytes([0x31, 0x11, 0x00, 0x00])
    with pytest.raises(ProxyProtocolError):
        parse_proxy_header(data)


def test_parse_rejects_bad_command():
    data = PP2_SIGNATURE + bytes([0x2F, 0x00, 0x00, 0x00])
    with pytest.raises(ProxyProtocolError):
        parse_proxy_header(data)


def test_parse_rejects_datagram():
    with pytest.raises(ProxyProtocolError):
        parse_proxy_header(pp2_header("1.2.3.4", "5.6.7.8", protocol=0x2))


def test_parse_rejects_unspecified_family():
    data = (
        PP2_SIGNATURE
        + bytes([0x21, 0x01])
        + struct.pack("!H", 12)
        + b"\x00" * 12
    )
    with pytest.raises(ProxyProtocolError):
        parse_proxy_header(data)


def test_parse_rejects_unix_family():
    data = (
        PP2_SIGNATURE
        + bytes([0x21, 0x31])
        + struct.pack("!H", 216)
        + b"\x00" * 216
    )
    with pytest.raises(ProxyProtocolError):
        parse_proxy_header(data)


def test_parse_rejects_overlong_header():
    data = PP2_SIGNATURE + bytes([0x21, 0x11]) + struct.pack("!H", 60000)
    with pytest.raises(ProxyProtocolError):
        parse_proxy_header(data)


@pytest.mark.parametrize("bad_len", (0, 11, 13, 36))
def test_parse_rejects_length_mismatch(bad_len):
    data = (
        PP2_SIGNATURE
        + bytes([0x21, 0x11])
        + struct.pack("!H", bad_len)
        + b"\x00" * bad_len
    )
    with pytest.raises(ProxyProtocolError):
        parse_proxy_header(data)


def test_parse_declared_payload_incomplete_waits():
    header = pp2_header("1.2.3.4", "5.6.7.8")
    # Fixed header declares 12 bytes but only 4 have arrived
    parsed, _ = parse_proxy_header(header[: PP2_FIXED_HEADER_LEN + 4])
    assert parsed is None


# -------------------------------------------------------------------------- #
# Live server integration tests
# -------------------------------------------------------------------------- #


async def _start_server(app, port, **kwargs):
    server = await app.create_server(
        host=HOST,
        port=port,
        return_asyncio_server=True,
        **kwargs,
    )
    await server.startup()
    await server.before_start()
    await server.after_start()
    return server


async def _stop_server(server):
    await server.before_stop()
    close_task = server.close()
    if close_task:
        await close_task
    for connection in list(server.connections):
        connection.close_if_idle()
    await server.after_stop()


def _register_routes(app):
    @app.get("/")
    async def index(request):
        return sanic_json(
            {
                "ip": request.ip,
                "port": request.port,
                "client_ip": request.client_ip,
                "socket": list(request.socket),
                "server_port": request.conn_info.server_port,
                "xff": request.remote_addr,
            }
        )

    @app.websocket("/ws")
    async def ws_handler(request, ws):
        while True:
            message = await ws.recv()
            await ws.send(f"{request.ip}:{message}")


@pytest.fixture
async def make_proxy_server(app, port):
    servers = []

    async def make(ssl_context=None, **config):
        app.config.PROXY_PROTOCOL = True
        for key, value in config.items():
            setattr(app.config, key, value)
        _register_routes(app)
        server = await _start_server(app, port, ssl=ssl_context)
        servers.append(server)
        return app, port, ssl_context is not None

    yield make

    for server in servers:
        await _stop_server(server)


async def _raw_request(port, data, timeout=3.0):
    reader, writer = await asyncio.open_connection(HOST, port)
    writer.write(data)
    await writer.drain()
    try:
        response = await asyncio.wait_for(reader.read(65535), timeout)
    except (
        ConnectionError,
        asyncio.IncompleteReadError,
        asyncio.TimeoutError,
    ):
        response = b""
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionError:
        pass
    return response


async def _fragmented_raw_request(port, data, delay=0.0005):
    reader, writer = await asyncio.open_connection(HOST, port)
    for chunk in data:
        writer.write(bytes([chunk]))
        await writer.drain()
        await asyncio.sleep(delay)
    try:
        response = await asyncio.wait_for(reader.read(65535), 5.0)
    except (ConnectionError, asyncio.IncompleteReadError):
        response = b""
    writer.close()
    return response


def _json_body(response: bytes) -> dict:
    return json.loads(response.split(b"\r\n\r\n", 1)[1])


@pytest.mark.asyncio
async def test_proxy_v4_addresses(make_proxy_server):
    _, port, _ = await make_proxy_server()
    response = await _raw_request(
        port, pp2_header("9.9.9.9", "10.0.0.1", 4321, 80) + HTTP_REQUEST
    )
    assert response.startswith(b"HTTP/1.1 200")
    body = _json_body(response)
    assert body["ip"] == "9.9.9.9"
    assert body["port"] == 4321
    assert body["client_ip"] == "9.9.9.9"
    assert body["socket"] == ["9.9.9.9", 4321]
    assert body["server_port"] == 80


@pytest.mark.asyncio
async def test_proxy_v6_addresses(make_proxy_server):
    _, port, _ = await make_proxy_server()
    response = await _raw_request(
        port,
        pp2_header("2001:db8::abcd", "2001:db8::1", 4321, 80) + HTTP_REQUEST,
    )
    assert response.startswith(b"HTTP/1.1 200")
    body = _json_body(response)
    assert body["ip"] == "2001:db8::abcd"
    assert body["port"] == 4321


@pytest.mark.asyncio
async def test_local_command_keeps_underlying_address(make_proxy_server):
    _, port, _ = await make_proxy_server()
    response = await _raw_request(port, pp2_local() + HTTP_REQUEST)
    assert response.startswith(b"HTTP/1.1 200")
    body = _json_body(response)
    # The client really is this process over loopback
    assert body["ip"] == "127.0.0.1"
    assert body["client_ip"] == "127.0.0.1"
    assert body["port"] > 1024
    assert body["socket"] == ["127.0.0.1", body["port"]]


@pytest.mark.asyncio
async def test_bytes_after_header_are_http(make_proxy_server):
    _, port, _ = await make_proxy_server()
    response = await _raw_request(
        port, pp2_header("9.9.9.9", "10.0.0.1") + HTTP_REQUEST
    )
    assert b"200 OK" in response


@pytest.mark.asyncio
async def test_fragmented_header_byte_by_byte(make_proxy_server):
    _, port, _ = await make_proxy_server()
    data = pp2_header("8.8.4.4", "10.0.0.1", 4567) + HTTP_REQUEST
    response = await _fragmented_raw_request(port, data)
    assert response.startswith(b"HTTP/1.1 200")
    body = _json_body(response)
    assert body["ip"] == "8.8.4.4"
    assert body["port"] == 4567


@pytest.mark.asyncio
async def test_malformed_header_closes_without_response(make_proxy_server):
    _, port, _ = await make_proxy_server()
    response = await _raw_request(
        port, b"GET / HTTP/1.1\r\nHost: evil\r\n\r\n"
    )
    assert response == b""


@pytest.mark.asyncio
async def test_bad_version_closes_connection(make_proxy_server):
    _, port, _ = await make_proxy_server()
    data = (
        PP2_SIGNATURE
        + bytes([0x31, 0x11])
        + struct.pack("!H", 12)
        + b"\x00" * 12
        + HTTP_REQUEST
    )
    assert await _raw_request(port, data) == b""


@pytest.mark.asyncio
async def test_datagram_header_closes_connection(make_proxy_server):
    _, port, _ = await make_proxy_server()
    assert (
        await _raw_request(
            port,
            pp2_header("1.2.3.4", "5.6.7.8", protocol=0x2) + HTTP_REQUEST,
        )
        == b""
    )


@pytest.mark.asyncio
async def test_overlong_header_closes_immediately(make_proxy_server):
    _, port, _ = await make_proxy_server()
    data = (
        PP2_SIGNATURE
        + bytes([0x21, 0x11])
        + struct.pack("!H", 60000)
        + b"\x00" * 32
    )
    assert await _raw_request(port, data) == b""


@pytest.mark.asyncio
async def test_truncated_header_then_close_is_clean(make_proxy_server):
    _, port, _ = await make_proxy_server()
    reader, writer = await asyncio.open_connection(HOST, port)
    # Declares 12 address bytes but only sends 4 of them
    header = pp2_header("1.2.3.4", "5.6.7.8")
    writer.write(header[: PP2_FIXED_HEADER_LEN + 4])
    await writer.drain()
    await asyncio.sleep(0.1)
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionError:
        pass

    # The worker keeps serving subsequent connections normally
    response = await _raw_request(
        port, pp2_header("9.9.9.9", "10.0.0.1") + HTTP_REQUEST
    )
    assert response.startswith(b"HTTP/1.1 200")
    assert _json_body(response)["ip"] == "9.9.9.9"


@pytest.mark.asyncio
async def test_malformed_connections_do_not_affect_later_ones(
    make_proxy_server,
):
    _, port, _ = await make_proxy_server()
    for bad in (
        b"garbage",
        PP2_SIGNATURE + bytes([0x2F, 0x00, 0x00, 0x00]),
        PP2_SIGNATURE + bytes([0x21, 0x11]) + struct.pack("!H", 60000),
    ):
        assert await _raw_request(port, bad) == b""

    response = await _raw_request(
        port, pp2_header("3.3.3.3", "10.0.0.1") + HTTP_REQUEST
    )
    assert response.startswith(b"HTTP/1.1 200")
    assert _json_body(response)["ip"] == "3.3.3.3"


@pytest.mark.asyncio
async def test_shutdown_closes_connection_with_pending_header(app, port):
    app.config.PROXY_PROTOCOL = True
    _register_routes(app)
    server = await _start_server(app, port)

    # Connect but never complete the PROXY header
    reader, writer = await asyncio.open_connection(HOST, port)
    writer.write(PP2_SIGNATURE[:8])
    await writer.drain()
    await asyncio.sleep(0.1)
    assert len(server.connections) == 1

    await _stop_server(server)

    # Graceful shutdown aborts the connection waiting for its header
    data = await asyncio.wait_for(reader.read(1), timeout=2)
    assert data == b""
    writer.close()


async def _read_one_response(reader):
    headers = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in headers.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = await reader.readexactly(length)
    return headers, body


@pytest.mark.asyncio
async def test_keep_alive_reuses_proxy_connection(make_proxy_server):
    _, port, _ = await make_proxy_server()
    reader, writer = await asyncio.open_connection(HOST, port)
    writer.write(pp2_header("4.3.2.1", "10.0.0.1", 9999))
    await writer.drain()

    writer.write(b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n")
    await writer.drain()
    headers, body = await _read_one_response(reader)
    assert b"200 OK" in headers
    assert json.loads(body)["ip"] == "4.3.2.1"

    # Second request on the same connection needs no second PROXY header
    writer.write(
        b"GET / HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n"
    )
    await writer.drain()
    headers, body = await _read_one_response(reader)
    assert b"200 OK" in headers
    assert json.loads(body)["ip"] == "4.3.2.1"

    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionError:
        pass


@pytest.mark.asyncio
async def test_xforwarded_headers_still_apply_on_top(make_proxy_server):
    _, port, _ = await make_proxy_server(PROXIES_COUNT=1)
    request = (
        b"GET / HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"X-Forwarded-For: 203.0.113.7\r\n"
        b"Connection: close\r\n\r\n"
    )
    response = await _raw_request(
        port, pp2_header("9.9.9.9", "10.0.0.1") + request
    )
    body = _json_body(response)
    # PROXY header is the transport peer ...
    assert body["ip"] == "9.9.9.9"
    # ... while the header-based proxy chain is honored as before
    assert body["client_ip"] == "203.0.113.7"
    assert body["xff"] == "203.0.113.7"


async def _tls_request(port, preamble, request=HTTP_REQUEST):
    raw = socket.create_connection((HOST, port))
    raw.sendall(preamble)
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection(
        sock=raw, ssl=context, server_hostname="localhost"
    )
    writer.write(request)
    await writer.drain()
    response = await reader.read(65535)
    writer.close()
    return response


@pytest.mark.asyncio
async def test_proxy_header_over_tls(make_proxy_server):
    context = CertSimple(CERT_FILE, KEY_FILE, names=["localhost"])
    _, port, _ = await make_proxy_server(ssl_context=context)
    response = await _tls_request(
        port, pp2_header("7.7.7.7", "10.0.0.1", dst_port=443)
    )
    assert response.startswith(b"HTTP/1.1 200")
    body = _json_body(response)
    assert body["ip"] == "7.7.7.7"


@pytest.mark.asyncio
async def test_local_header_over_tls(make_proxy_server):
    context = CertSimple(CERT_FILE, KEY_FILE, names=["localhost"])
    _, port, _ = await make_proxy_server(ssl_context=context)
    response = await _tls_request(port, pp2_local())
    assert response.startswith(b"HTTP/1.1 200")
    assert _json_body(response)["ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_tls_without_header_is_rejected(make_proxy_server):
    context = CertSimple(CERT_FILE, KEY_FILE, names=["localhost"])
    _, port, _ = await make_proxy_server(ssl_context=context)
    raw = socket.create_connection((HOST, port))
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        reader, writer = await asyncio.open_connection(
            sock=raw, ssl=context, server_hostname="localhost"
        )
        await writer.drain()
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_websocket_after_proxy_header(make_proxy_server):
    from websockets.client import WebSocketClientProtocol
    from websockets.uri import parse_uri

    _, port, _ = await make_proxy_server()
    raw = socket.create_connection((HOST, port))
    raw.sendall(pp2_header("5.4.3.2", "10.0.0.1"))
    loop = asyncio.get_running_loop()
    _transport, protocol = await loop.create_connection(
        lambda: WebSocketClientProtocol(), sock=raw
    )
    await protocol.handshake(parse_uri(f"ws://{HOST}:{port}/ws"))
    await protocol.send("hello")
    reply = await asyncio.wait_for(protocol.recv(), timeout=3)
    assert reply == "5.4.3.2:hello"
    await protocol.close()


# -------------------------------------------------------------------------- #
# Disabled mode keeps the original behavior
# -------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disabled_mode_treats_bytes_as_http(app, port):
    app.config.PROXY_PROTOCOL = False
    _register_routes(app)
    server = await _start_server(app, port)
    try:
        # Raw PROXY magic is just invalid HTTP and rejected as such
        bad = await _raw_request(
            port,
            pp2_header("9.9.9.9", "10.0.0.1") + HTTP_REQUEST,
        )
        assert b"200 OK" not in bad

        # A normal HTTP request still works with the real peer address
        good = await _raw_request(port, HTTP_REQUEST)
        assert good.startswith(b"HTTP/1.1 200")
        body = _json_body(good)
        assert body["ip"] == "127.0.0.1"
    finally:
        await _stop_server(server)


# -------------------------------------------------------------------------- #
# Configuration validation and worker wiring
# -------------------------------------------------------------------------- #


def test_proxy_protocol_rejected_with_unix_socket(app):
    app.config.PROXY_PROTOCOL = True
    with pytest.raises(ServerError):
        app._helper(unix="/tmp/sanic-test-proxy.sock")


def test_proxy_protocol_defaults_to_disabled(app):
    assert app.config.PROXY_PROTOCOL is False


@pytest.mark.asyncio
async def test_worker_factory_wiring_enabled(app, port, monkeypatch):
    app.config.PROXY_PROTOCOL = True
    ssl_context = CertSimple(CERT_FILE, KEY_FILE, names=["localhost"])
    captured = {}
    loop = asyncio.get_running_loop()
    original = loop.create_server

    async def spy(*args, **kwargs):
        captured["factory"] = args[0]
        captured["ssl"] = kwargs["ssl"]
        kwargs["start_serving"] = False
        return await original(*args, **kwargs)

    monkeypatch.setattr(loop, "create_server", spy)
    server = await app.create_server(
        host=HOST,
        port=port,
        ssl=ssl_context,
        return_asyncio_server=True,
    )
    await server.startup()
    try:
        assert captured["factory"].func is ProxyProtocol
        # TLS is started by the wrapper, not the listening socket
        assert captured["ssl"] is None
    finally:
        close_task = server.close()
        if close_task:
            await close_task


@pytest.mark.asyncio
async def test_worker_factory_wiring_disabled(app, port, monkeypatch):
    app.config.PROXY_PROTOCOL = False
    captured = {}
    loop = asyncio.get_running_loop()
    original = loop.create_server

    async def spy(*args, **kwargs):
        captured["factory"] = args[0]
        captured["ssl"] = kwargs["ssl"]
        kwargs["start_serving"] = False
        return await original(*args, **kwargs)

    monkeypatch.setattr(loop, "create_server", spy)
    server = await app.create_server(
        host=HOST, port=port, return_asyncio_server=True
    )
    await server.startup()
    try:
        assert captured["factory"].func is HttpProtocol
        assert captured["ssl"] is None
    finally:
        close_task = server.close()
        if close_task:
            await close_task


# -------------------------------------------------------------------------- #
# Multiple workers share the same configuration
# -------------------------------------------------------------------------- #


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="Multiple workers require a fork-capable platform",
)
def test_proxy_protocol_multiple_workers(app, port):
    app.config.PROXY_PROTOCOL = True

    @app.get("/")
    async def handler(request):
        return text(f"ip={request.ip}")

    marker = os.path.join(
        tempfile.gettempdir(), f"sanic-proxy-test-{port}.marker"
    )
    try:
        os.unlink(marker)
    except FileNotFoundError:
        pass

    @app.after_server_start
    async def verify(app, loop):
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return
        result = "fail: never completed"
        try:
            await asyncio.sleep(0.5)
            reader, writer = await asyncio.open_connection(HOST, port)
            writer.write(
                pp2_header("9.9.9.9", "10.0.0.1")
                + b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            result = (
                "ok" if b"ip=9.9.9.9" in response else "fail: bad response"
            )
        except Exception as e:  # propagate the outcome to the main process
            result = f"fail: {e!r}"
        finally:
            os.write(fd, result.encode())
            os.close(fd)
            app.stop()

    with use_context("fork"):
        app.run(HOST, port, workers=2)

    with open(marker, "rb") as f:
        outcome = f.read().decode()
    try:
        os.unlink(marker)
    except FileNotFoundError:
        pass
    assert outcome == "ok", outcome
