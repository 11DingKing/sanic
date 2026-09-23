from __future__ import annotations

import asyncio
import warnings

from contextlib import suppress
from typing import TYPE_CHECKING

from sanic.compat import Header
from sanic.exceptions import BadRequest, RequestCancelled, ServerError
from sanic.helpers import Default
from sanic.http import Stage
from sanic.log import error_logger, logger
from sanic.models.asgi import (
    ASGIMessage,
    ASGIReceive,
    ASGIScope,
    ASGISend,
    MockTransport,
)
from sanic.request import Request
from sanic.response import BaseHTTPResponse
from sanic.server import ConnInfo
from sanic.server.websockets.connection import WebSocketConnection


if TYPE_CHECKING:
    from sanic import Sanic


class Lifespan:
    def __init__(
        self, sanic_app, scope: ASGIScope, receive: ASGIReceive, send: ASGISend
    ) -> None:
        self.sanic_app = sanic_app
        self.scope = scope
        self.receive = receive
        self.send = send

        if "server.init.before" in self.sanic_app.signal_router.name_index:
            logger.debug(
                'You have set a listener for "before_server_start" '
                "in ASGI mode. "
                "It will be executed as early as possible, but not before "
                "the ASGI server is started.",
                extra={"verbosity": 1},
            )
        if "server.shutdown.after" in self.sanic_app.signal_router.name_index:
            logger.debug(
                'You have set a listener for "after_server_stop" '
                "in ASGI mode. "
                "It will be executed as late as possible, but not after "
                "the ASGI server is stopped.",
                extra={"verbosity": 1},
            )

    async def startup(self) -> None:
        """
        Gather the listeners to fire on server start.
        Because we are using a third-party server and not Sanic server, we do
        not have access to fire anything BEFORE the server starts.
        Therefore, we fire before_server_start and after_server_start
        in sequence since the ASGI lifespan protocol only supports a single
        startup event.
        """
        await self.sanic_app._startup()
        await self.sanic_app._server_event("init", "before")
        await self.sanic_app._server_event("init", "after")

        if not isinstance(self.sanic_app.config.USE_UVLOOP, Default):
            warnings.warn(
                "You have set the USE_UVLOOP configuration option, but Sanic "
                "cannot control the event loop when running in ASGI mode."
                "This option will be ignored."
            )

    async def shutdown(self) -> None:
        """
        Gather the listeners to fire on server stop.
        Because we are using a third-party server and not Sanic server, we do
        not have access to fire anything AFTER the server stops.
        Therefore, we fire before_server_stop and after_server_stop
        in sequence since the ASGI lifespan protocol only supports a single
        shutdown event.
        """
        await self.sanic_app._server_event("shutdown", "before")
        await self.sanic_app._server_event("shutdown", "after")

    async def __call__(self) -> None:
        while True:
            message = await self.receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.startup()
                except Exception as e:
                    error_logger.exception(e)
                    await self.send(
                        {"type": "lifespan.startup.failed", "message": str(e)}
                    )
                else:
                    await self.send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                try:
                    await self.shutdown()
                except Exception as e:
                    error_logger.exception(e)
                    await self.send(
                        {"type": "lifespan.shutdown.failed", "message": str(e)}
                    )
                else:
                    await self.send({"type": "lifespan.shutdown.complete"})
                return


class ASGIApp:
    sanic_app: Sanic
    request: Request
    transport: MockTransport
    lifespan: Lifespan
    ws: WebSocketConnection | None
    stage: Stage
    response: BaseHTTPResponse | None
    request_body: bool
    _body_messages: asyncio.Queue[ASGIMessage]
    _terminated: asyncio.Event
    _handler_task: asyncio.Task | None
    _receiver_task: asyncio.Task | None

    @classmethod
    async def create(
        cls,
        sanic_app: Sanic,
        scope: ASGIScope,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> ASGIApp:
        instance = cls()
        instance.ws = None
        instance.sanic_app = sanic_app
        instance.transport = MockTransport(scope, receive, send)
        instance.transport.loop = sanic_app.loop
        instance.stage = Stage.IDLE
        instance.response = None
        instance.sanic_app.state.is_started = True
        setattr(instance.transport, "add_task", sanic_app.loop.create_task)

        try:
            headers = Header(
                [
                    (
                        key.decode("ASCII"),
                        value.decode(errors="surrogateescape"),
                    )
                    for key, value in scope.get("headers", [])
                ]
            )
        except UnicodeDecodeError:
            raise BadRequest(
                "Header names can only contain US-ASCII characters"
            )

        if scope["type"] == "http":
            version = scope["http_version"]
            method = scope["method"]
        elif scope["type"] == "websocket":
            version = "1.1"
            method = "GET"

            instance.ws = instance.transport.create_websocket_connection(
                send, receive
            )
        else:
            raise ServerError("Received unknown ASGI scope")

        url_bytes, query = scope["raw_path"], scope["query_string"]
        if query:
            # httpx ASGI client sends query string as part of raw_path
            url_bytes = url_bytes.split(b"?", 1)[0]
            # All servers send them separately
            url_bytes = b"%b?%b" % (url_bytes, query)

        request_class = sanic_app.request_class or Request  # type: ignore
        instance.request = request_class(
            url_bytes,
            headers,
            version,
            method,
            instance.transport,
            sanic_app,
        )
        request_class._current.set(instance.request)
        instance.request.stream = instance  # type: ignore
        instance.request_body = True
        instance._terminated = asyncio.Event()
        instance._body_messages = asyncio.Queue(maxsize=1)
        instance._handler_task = None
        instance._receiver_task = None
        instance.request.conn_info = ConnInfo(instance.transport)

        await instance.sanic_app.dispatch(
            "http.lifecycle.request",
            inline=True,
            context={"request": instance.request},
            fail_not_found=False,
        )

        return instance

    def _terminate(self) -> None:
        """Share the termination state with every part of the cycle.

        Once the client has disconnected the same state is observed by the
        ASGI receive watcher, the request stream, the (possibly still
        running) handler and the response sender: nobody waits for more
        body messages and no further responses are written.
        """
        if not self._terminated.is_set():
            self._terminated.set()
            self.request_body = False
        if (
            self._handler_task
            and not self._handler_task.done()
            and self._handler_task is not asyncio.current_task()
        ):
            # The watcher observed the disconnect while the handler was
            # busy elsewhere. When termination is raised from within the
            # handler (e.g. while reading the body) the RequestCancelled
            # below is already a clear failure result.
            self._handler_task.cancel()

    async def _watch_receive(self) -> None:
        """Sole consumer of the ASGI ``receive`` channel.

        ``http.request`` messages are handed to ``read`` one at a time so
        that backpressure is preserved, while an ``http.disconnect``
        immediately terminates the request and cancels the handler.
        """
        try:
            while True:
                message = await self.transport.receive()
                if message.get("type") == "http.disconnect":
                    self._terminate()
                    return
                await self._body_messages.put(message)
        except asyncio.CancelledError:
            return
        except Exception:
            # The receive channel failed. Terminating is safer than
            # leaving the handler waiting on the body queue forever.
            self._terminate()

    async def _receive_message(self) -> ASGIMessage:
        """Wait for the next body message or for termination."""
        body_task: asyncio.Task = asyncio.ensure_future(
            self._body_messages.get()
        )
        terminate_task: asyncio.Task = asyncio.ensure_future(
            self._terminated.wait()
        )
        try:
            await asyncio.wait(
                {body_task, terminate_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            body_task.cancel()
            terminate_task.cancel()
            # The watcher cancels the handler at the same time as it
            # observes the disconnect. Surface the explicit failure at
            # the request stream instead of a bare cancellation.
            if self._terminated.is_set():
                raise RequestCancelled() from None
            raise

        if body_task.done():
            # Prefer a body message that arrived together with the
            # termination so that it is not silently dropped.
            terminate_task.cancel()
            return body_task.result()

        body_task.cancel()
        with suppress(asyncio.CancelledError):
            await body_task
        raise RequestCancelled()

    async def read(self) -> bytes | None:
        """
        Read and stream the body in chunks from an incoming ASGI message.
        """
        if self._terminated.is_set():
            raise RequestCancelled()
        if self.stage is Stage.IDLE:
            self.stage = Stage.REQUEST
        if self._receiver_task is None:
            # Non-HTTP scopes do not run the receive watcher and keep
            # consuming the channel directly.
            message = await self.transport.receive()
        else:
            message = await self._receive_message()
        if message.get("type") == "http.disconnect":
            self._terminate()
            raise RequestCancelled()
        body = message.get("body", b"")
        if not message.get("more_body", False):
            self.request_body = False
            if not body:
                return None
        return body

    async def __aiter__(self):
        while self.request_body:
            data = await self.read()
            if data:
                yield data

    def respond(self, response: BaseHTTPResponse):
        if self.stage is not Stage.HANDLER:
            self.stage = Stage.FAILED
            raise RuntimeError("Response already started")
        if self.response is not None:
            self.response.stream = None
        response.stream, self.response = self, response
        return response

    async def send(self, data, end_stream):
        if self._terminated.is_set():
            raise RequestCancelled()
        if self.stage is Stage.IDLE:
            if not end_stream or data:
                raise RuntimeError(
                    "There is no request to respond to, either the "
                    "response has already been sent or the "
                    "request has not been received yet."
                )
            return
        try:
            if self.response and self.stage is Stage.HANDLER:
                await self.transport.send(
                    {
                        "type": "http.response.start",
                        "status": self.response.status,
                        "headers": self.response.processed_headers,
                    }
                )
                response_body = getattr(self.response, "body", None)
                if response_body:
                    data = response_body + data if data else response_body
            self.stage = Stage.IDLE if end_stream else Stage.RESPONSE
            await self.transport.send(
                {
                    "type": "http.response.body",
                    "body": data.encode() if hasattr(data, "encode") else data,
                    "more_body": not end_stream,
                }
            )
        except asyncio.CancelledError:
            # The ASGI server cancelled a write, typically because the
            # client went away. Normalize it to an explicit failure and
            # make sure no further writes are attempted.
            self._terminate()
            raise RequestCancelled from None
        except Exception as exc:
            # Per the ASGI spec a failure while sending indicates that
            # the response did not reach the client.
            self._terminate()
            raise RequestCancelled from exc

    _asgi_single_callable = True  # We conform to ASGI 3.0 single-callable

    async def __call__(self) -> None:
        """
        Handle the incoming request.
        """
        loop = asyncio.get_running_loop()
        if self.transport.scope.get("type") == "http":
            self._receiver_task = loop.create_task(self._watch_receive())

        self.stage = Stage.HANDLER
        self._handler_task = loop.create_task(
            self.sanic_app.handle_request(self.request)
        )
        try:
            await self._handler_task
        except asyncio.CancelledError:
            if self._terminated.is_set():
                logger.debug(
                    "Request: %s %s stopped. Client disconnected.",
                    self.request.method,
                    self.request.url,
                )
                return
            raise
        except Exception as e:
            try:
                await self.sanic_app.handle_exception(self.request, e)
            except asyncio.CancelledError:
                if self._terminated.is_set():
                    return
                raise
            except Exception as exc:
                await self.sanic_app.handle_exception(self.request, exc, False)
        finally:
            # If the call itself was torn down (e.g. server shutdown)
            # the handler, which runs as a task, must not be left behind.
            if not self._handler_task.done():
                self._handler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._handler_task
            if self._receiver_task is not None:
                self._receiver_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._receiver_task
                self._receiver_task = None
