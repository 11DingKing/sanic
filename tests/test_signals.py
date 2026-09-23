import asyncio

from enum import Enum
from itertools import count

import pytest

from sanic_routing.exceptions import NotFound

from sanic import Blueprint, Sanic, empty
from sanic.exceptions import InvalidSignal, SanicException
from sanic.signals import Event


def test_add_signal(app):
    def sync_signal(*_): ...

    app.add_signal(sync_signal, "foo.bar.baz")

    assert len(app.signal_router.routes) == 1


def test_add_signal_method_handler(app):
    counter = 0

    class TestSanic(Sanic):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.add_signal(
                self.after_routing_signal_handler, "http.routing.after"
            )

        def after_routing_signal_handler(self, *args, **kwargs):
            nonlocal counter
            counter += 1

    app = TestSanic("Test")
    assert len(app.signal_router.routes) == 1

    @app.route("/")
    async def handler(_):
        return empty()

    app.test_client.get("/")
    assert counter == 1


def test_add_signal_decorator(app):
    @app.signal("foo.bar.baz")
    def sync_signal(*_): ...

    @app.signal("foo.bar.baz")
    async def async_signal(*_): ...

    assert len(app.signal_router.routes) == 2
    assert len(app.signal_router.dynamic_routes) == 1


@pytest.mark.parametrize(
    "signal",
    (
        "<foo>.bar.bax",
        "foo.<bar>.baz",
        "foo.bar",
        "foo.bar.baz.qux",
    ),
)
def test_invalid_signal(app, signal):
    with pytest.raises(InvalidSignal, match=f"Invalid signal event: {signal}"):

        @app.signal(signal)
        def handler(): ...


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_event(app):
    @app.signal("foo.bar.baz")
    def sync_signal(*args):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(app.event("foo.bar.baz"))
    await app.dispatch("foo.bar.baz")
    await asyncio.sleep(0)

    assert event_task.done()
    event_task.result()  # Will raise if there was an exception


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_correct_event(app):
    # Check for https://github.com/sanic-org/sanic/issues/2826

    @app.signal("foo.bar.baz")
    def sync_signal_baz(*args):
        pass

    @app.signal("foo.bar.spam")
    def sync_signal_spam(*args):
        pass

    app.signal_router.finalize()

    baz_task = asyncio.create_task(app.event("foo.bar.baz"))
    spam_task = asyncio.create_task(app.event("foo.bar.spam"))

    await app.dispatch("foo.bar.baz")
    await asyncio.sleep(0)

    assert baz_task.done()
    assert not spam_task.done()
    baz_task.result()
    spam_task.cancel()


@pytest.mark.asyncio
async def test_dispatch_signal_with_enum_event(app):
    counter = 0

    class FooEnum(Enum):
        FOO_BAR_BAZ = "foo.bar.baz"

    @app.signal(FooEnum.FOO_BAR_BAZ)
    def sync_signal(*_):
        nonlocal counter

        counter += 1

    app.signal_router.finalize()

    await app.dispatch("foo.bar.baz")
    assert counter == 1


@pytest.mark.asyncio
async def test_dispatch_signal_with_enum_event_to_event(app):
    class FooEnum(Enum):
        FOO_BAR_BAZ = "foo.bar.baz"

    @app.signal(FooEnum.FOO_BAR_BAZ)
    def sync_signal(*args):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(app.event(FooEnum.FOO_BAR_BAZ))
    await app.dispatch("foo.bar.baz")
    await asyncio.sleep(0)

    assert event_task.done()
    event_task.result()  # Will raise if there was an exception


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_multiple_handlers(app):
    counter = 0

    @app.signal("foo.bar.baz")
    def sync_signal(*_):
        nonlocal counter

        counter += 1

    @app.signal("foo.bar.baz")
    async def async_signal(*_):
        nonlocal counter

        counter += 1

    app.signal_router.finalize()

    assert len(app.signal_router.routes) == 3
    await app.dispatch("foo.bar.baz")
    assert counter == 2


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_multiple_events(app):
    @app.signal("foo.bar.baz")
    def sync_signal(*_):
        pass

    app.signal_router.finalize()

    event_task1 = asyncio.create_task(app.event("foo.bar.baz"))
    event_task2 = asyncio.create_task(app.event("foo.bar.baz"))

    await app.dispatch("foo.bar.baz")
    await asyncio.sleep(0)

    assert event_task1.done()
    assert event_task2.done()
    event_task1.result()  # Will raise if there was an exception
    event_task2.result()  # Will raise if there was an exception


@pytest.mark.asyncio
async def test_dispatch_signal_with_multiple_handlers_triggers_event_once(app):
    @app.signal("foo.bar.baz")
    def sync_signal(*_):
        pass

    @app.signal("foo.bar.baz")
    async def async_signal(*_):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(app.event("foo.bar.baz"))
    await app.dispatch("foo.bar.baz")
    await asyncio.sleep(0)

    assert event_task.done()
    event_task.result()  # Will raise if there was an exception


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_dynamic_route(app):
    counter = 0

    @app.signal("foo.bar.<baz:int>")
    def sync_signal(baz):
        nonlocal counter

        counter += baz

    app.signal_router.finalize()

    await app.dispatch("foo.bar.9")
    assert counter == 9


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_parameterized_dynamic_route_event(app):
    @app.signal("foo.bar.<baz:int>")
    def sync_signal(baz):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(app.event("foo.bar.<baz:int>"))
    await app.dispatch("foo.bar.9")
    await asyncio.sleep(0)

    assert event_task.done()
    event_task.result()  # Will raise if there was an exception


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_starred_dynamic_route_event(app):
    @app.signal("foo.bar.<baz:int>")
    def sync_signal(baz):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(app.event("foo.bar.*"))
    await app.dispatch("foo.bar.9")
    await asyncio.sleep(0)

    assert event_task.done()
    event_task.result()  # Will raise if there was an exception


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_with_requirements(app):
    counter = 0

    @app.signal("foo.bar.baz", condition={"one": "two"})
    def sync_signal(*_):
        nonlocal counter
        counter += 1

    app.signal_router.finalize()

    await app.dispatch("foo.bar.baz")
    assert counter == 0
    await app.dispatch("foo.bar.baz", condition={"one": "two"})
    assert counter == 1


@pytest.mark.asyncio
async def test_dispatch_signal_to_event_with_requirements(app):
    @app.signal("foo.bar.baz")
    def sync_signal(*_):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(
        app.event("foo.bar.baz", condition={"one": "two"})
    )
    await app.dispatch("foo.bar.baz")
    await asyncio.sleep(0)
    assert not event_task.done()

    await app.dispatch("foo.bar.baz", condition={"one": "two"})
    await asyncio.sleep(0)
    assert event_task.done()
    event_task.result()  # Will raise if there was an exception


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_with_requirements_exclusive(app):
    counter = 0

    @app.signal("foo.bar.baz", condition={"one": "two"}, exclusive=False)
    def sync_signal(*_):
        nonlocal counter
        counter += 1

    app.signal_router.finalize()

    await app.dispatch("foo.bar.baz")
    assert counter == 1
    await app.dispatch("foo.bar.baz", condition={"one": "two"})
    assert counter == 2


@pytest.mark.asyncio
async def test_dispatch_signal_to_event_with_requirements_exclusive(app):
    @app.signal("foo.bar.baz")
    def sync_signal(*_):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(
        app.event("foo.bar.baz", condition={"one": "two"}, exclusive=False)
    )
    await app.dispatch("foo.bar.baz")
    await asyncio.sleep(0)
    assert event_task.done()
    event_task.result()  # Will raise if there was an exception

    event_task = asyncio.create_task(
        app.event("foo.bar.baz", condition={"one": "two"}, exclusive=False)
    )
    await app.dispatch("foo.bar.baz", condition={"one": "two"})
    await asyncio.sleep(0)
    assert event_task.done()
    event_task.result()  # Will raise if there was an exception


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_with_context(app):
    counter = 0

    @app.signal("foo.bar.baz")
    def sync_signal(amount):
        nonlocal counter
        counter += amount

    app.signal_router.finalize()

    await app.dispatch("foo.bar.baz", context={"amount": 9})
    assert counter == 9


@pytest.mark.asyncio
async def test_dispatch_signal_to_event_with_context(app):
    @app.signal("foo.bar.baz")
    def sync_signal(**context):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(app.event("foo.bar.baz"))
    await app.dispatch("foo.bar.baz", context={"amount": 9})
    await asyncio.sleep(0)
    assert event_task.done()
    assert event_task.result()["amount"] == 9


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_with_context_fail(app):
    counter = 0

    @app.signal("foo.bar.baz")
    def sync_signal(amount):
        nonlocal counter
        counter += amount

    app.signal_router.finalize()

    with pytest.raises(TypeError):
        await app.dispatch("foo.bar.baz", {"amount": 9})


@pytest.mark.asyncio
async def test_dispatch_signal_to_dynamic_route_event(app):
    @app.signal("foo.bar.<something>")
    def sync_signal(**context):
        pass

    app.signal_router.finalize()

    event_task = asyncio.create_task(app.event("foo.bar.<something>"))
    await app.dispatch("foo.bar.baz")
    await asyncio.sleep(0)
    assert event_task.done()
    assert event_task.result()["something"] == "baz"


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_on_bp(app):
    bp = Blueprint("bp")

    app_counter = 0
    bp_counter = 0

    @app.signal("foo.bar.baz")
    def app_signal():
        nonlocal app_counter
        app_counter += 1

    @bp.signal("foo.bar.baz")
    def bp_signal():
        nonlocal bp_counter
        bp_counter += 1

    app.blueprint(bp)
    app.signal_router.finalize()

    await app.dispatch("foo.bar.baz")
    assert app_counter == 1
    assert bp_counter == 1

    await bp.dispatch("foo.bar.baz")
    assert app_counter == 1
    assert bp_counter == 2


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_on_bp_alone(app):
    bp = Blueprint("bp")

    bp_counter = 0

    @bp.signal("foo.bar.baz")
    def bp_signal():
        nonlocal bp_counter
        bp_counter += 1

    app.blueprint(bp)
    app.signal_router.finalize()
    await app.dispatch("foo.bar.baz")
    await bp.dispatch("foo.bar.baz")
    assert bp_counter == 2


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_event_on_bp(app):
    bp = Blueprint("bp")

    @app.signal("foo.bar.baz")
    def app_signal(): ...

    @bp.signal("foo.bar.baz")
    def bp_signal(): ...

    app.blueprint(bp)
    app.signal_router.finalize()

    app_task = asyncio.create_task(app.event("foo.bar.baz"))
    bp_task = asyncio.create_task(bp.event("foo.bar.baz"))
    await asyncio.sleep(0)
    await app.dispatch("foo.bar.baz")

    # Allow a few event loop iterations for tasks to finish
    for _ in range(5):
        await asyncio.sleep(0)

    assert app_task.done()
    assert bp_task.done()
    app_task.result()
    bp_task.result()

    app_task = asyncio.create_task(app.event("foo.bar.baz"))
    bp_task = asyncio.create_task(bp.event("foo.bar.baz"))
    await asyncio.sleep(0)
    await bp.dispatch("foo.bar.baz")

    # Allow a few event loop iterations for tasks to finish
    for _ in range(5):
        await asyncio.sleep(0)

    assert bp_task.done()
    assert not app_task.done()
    bp_task.result()
    app_task.cancel()


@pytest.mark.asyncio
async def test_dispatch_simple_signal_triggers(app):
    counter = 0

    @app.signal("foo")
    def sync_signal():
        nonlocal counter

        counter += 1

    app.signal_router.finalize()

    await app.dispatch("foo")
    assert counter == 1


@pytest.mark.asyncio
async def test_dispatch_simple_signal_triggers_dynamic_foo(app):
    counter = 0

    @app.signal("<foo:int>")
    def sync_signal(foo):
        nonlocal counter

        counter += foo

    app.signal_router.finalize()

    await app.dispatch("9")
    assert counter == 9


@pytest.mark.asyncio
async def test_dispatch_simple_signal_triggers_foo_bar(app):
    counter = 0

    @app.signal("foo.bar.<baz:int>")
    def sync_signal(baz):
        nonlocal counter

        counter += baz

    app.signal_router.finalize()

    await app.dispatch("foo.bar.9")
    assert counter == 9


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_event_on_bp_with_context(app):
    bp = Blueprint("bp")

    @bp.signal("foo.bar.baz")
    def bp_signal(): ...

    app.blueprint(bp)
    app.signal_router.finalize()

    event_task = asyncio.create_task(bp.event("foo.bar.baz"))
    await asyncio.sleep(0)
    await app.dispatch("foo.bar.baz", context={"amount": 9})
    for _ in range(5):
        await asyncio.sleep(0)
    assert event_task.done()
    assert event_task.result()["amount"] == 9


def test_bad_finalize(app):
    counter = 0

    @app.signal("foo.bar.baz")
    def sync_signal(amount):
        nonlocal counter
        counter += amount

    with pytest.raises(
        RuntimeError, match="Cannot finalize signals outside of event loop"
    ):
        app.signal_router.finalize()

    assert counter == 0


@pytest.mark.asyncio
async def test_event_not_exist(app):
    with pytest.raises(NotFound, match="Could not find signal does.not.exist"):
        await app.event("does.not.exist")


@pytest.mark.asyncio
async def test_event_not_exist_on_bp(app):
    bp = Blueprint("bp")
    app.blueprint(bp)

    with pytest.raises(NotFound, match="Could not find signal does.not.exist"):
        await bp.event("does.not.exist")


@pytest.mark.asyncio
async def test_event_not_exist_with_autoregister(app):
    app.config.EVENT_AUTOREGISTER = True
    try:
        await app.event("does.not.exist", timeout=0.1)
    except asyncio.TimeoutError:
        ...


@pytest.mark.asyncio
async def test_dispatch_signal_triggers_non_exist_event_with_autoregister(app):
    @app.signal("some.stand.in")
    async def signal_handler(): ...

    app.config.EVENT_AUTOREGISTER = True
    app_counter = 0
    app.signal_router.finalize()

    async def do_wait():
        nonlocal app_counter
        await app.event("foo.bar.baz")
        app_counter += 1

    fut = asyncio.ensure_future(do_wait())
    await app.dispatch("foo.bar.baz")
    await fut

    assert app_counter == 1


@pytest.mark.asyncio
async def test_dispatch_not_exist(app):
    @app.signal("do.something.start")
    async def signal_handler(): ...

    app.signal_router.finalize()
    await app.dispatch("does.not.exist")


def test_event_on_bp_not_registered():
    bp = Blueprint("bp")

    @bp.signal("foo.bar.baz")
    def bp_signal(): ...

    with pytest.raises(
        SanicException,
        match="<Blueprint bp> has not yet been registered to an app",
    ):
        bp.event("foo.bar.baz")


@pytest.mark.parametrize(
    "event,expected",
    (
        ("foo.bar.baz", True),
        ("server.init.before", True),
        ("server.init.somethingelse", False),
        ("http.request.start", False),
        ("sanic.notice.anything", True),
    ),
)
def test_signal_reservation(app, event, expected):
    if not expected:
        with pytest.raises(
            InvalidSignal,
            match=f"Cannot declare reserved signal event: {event}",
        ):
            app.signal(event)(lambda: ...)
    else:
        app.signal(event)(lambda: ...)


@pytest.mark.asyncio
async def test_report_exception(app: Sanic):
    @app.report_exception
    async def catch_any_exception(app: Sanic, exception: Exception): ...

    @app.route("/")
    async def handler(request):
        1 / 0

    app.signal_router.finalize()

    registered_signal_handlers = [
        handler
        for handler, *_ in app.signal_router.get(
            Event.SERVER_EXCEPTION_REPORT.value
        )
    ]

    assert catch_any_exception in registered_signal_handlers


def test_report_exception_runs(app: Sanic):
    event = asyncio.Event()

    @app.report_exception
    async def catch_any_exception(app: Sanic, exception: Exception):
        event.set()

    @app.route("/")
    async def handler(request):
        1 / 0

    app.test_client.get("/")

    assert event.is_set()


def test_report_exception_runs_once_inline(app: Sanic):
    event = asyncio.Event()
    c = count()

    @app.report_exception
    async def catch_any_exception(app: Sanic, exception: Exception):
        event.set()
        next(c)

    @app.route("/")
    async def handler(request): ...

    @app.signal(Event.HTTP_ROUTING_AFTER.value)
    async def after_routing(**_):
        1 / 0

    app.test_client.get("/")

    assert event.is_set()
    assert next(c) == 1


def test_report_exception_runs_once_custom(app: Sanic):
    event = asyncio.Event()
    c = count()

    @app.report_exception
    async def catch_any_exception(app: Sanic, exception: Exception):
        event.set()
        next(c)

    @app.route("/")
    async def handler(request):
        await app.dispatch("one.two.three")
        return empty()

    @app.signal("one.two.three")
    async def one_two_three(**_):
        1 / 0

    app.test_client.get("/")

    assert event.is_set()
    assert next(c) == 1


def test_report_exception_runs_task(app: Sanic):
    c = count()

    async def task_1():
        next(c)

    async def task_2(app):
        next(c)

    @app.report_exception
    async def catch_any_exception(app: Sanic, exception: Exception):
        next(c)

    @app.route("/")
    async def handler(request):
        app.add_task(task_1)
        app.add_task(task_1())
        app.add_task(task_2)
        app.add_task(task_2(app))
        return empty()

    app.test_client.get("/")

    assert next(c) == 4


@pytest.mark.asyncio
async def test_dispatch_signal_runs_handlers_in_order(app):
    calls = []

    @app.signal("foo.bar.baz")
    async def first(**_):
        calls.append("first")

    @app.signal("foo.bar.baz")
    async def second(**_):
        calls.append("second")

    app.signal_router.finalize()

    await app.dispatch("foo.bar.baz", inline=True)
    assert calls == ["first", "second"]

    await app.dispatch("foo.bar.baz", inline=True, reverse=True)
    assert calls == ["first", "second", "second", "first"]


@pytest.mark.asyncio
async def test_dispatch_signal_continues_after_handler_exception(app):
    calls = []

    @app.signal("foo.bar.baz")
    async def first(**_):
        calls.append("first")
        raise ValueError("first failed")

    @app.signal("foo.bar.baz")
    async def second(**_):
        calls.append("second")

    @app.signal("foo.bar.baz")
    async def third(**_):
        calls.append("third")

    app.signal_router.finalize()

    with pytest.raises(ValueError, match="first failed"):
        await app.dispatch("foo.bar.baz", inline=True)

    # A failing handler must not swallow the remaining handlers, which are
    # still executed in order.
    assert calls == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_dispatch_signal_reports_every_failed_handler(app):
    calls = []
    reported = []

    @app.signal(Event.SERVER_EXCEPTION_REPORT)
    async def report(exception: Exception):
        reported.append(exception)

    @app.signal("foo.bar.baz")
    async def first(**_):
        calls.append("first")
        raise ValueError("first failed")

    @app.signal("foo.bar.baz")
    async def second(**_):
        calls.append("second")

    @app.signal("foo.bar.baz")
    async def third(**_):
        calls.append("third")
        raise RuntimeError("third failed")

    app.signal_router.finalize()

    with pytest.raises(ValueError, match="first failed") as excinfo:
        await app.dispatch("foo.bar.baz", inline=True)

    assert calls == ["first", "second", "third"]

    # The exception report handlers run as a background task; allow the
    # loop to drain it.
    for _ in range(10):
        await asyncio.sleep(0)

    assert [type(exception) for exception in reported] == [
        ValueError,
        RuntimeError,
    ]
    assert [str(exception) for exception in reported] == [
        "first failed",
        "third failed",
    ]
    assert all(
        getattr(exception, "__dispatched__", False) for exception in reported
    )

    suppressed = getattr(excinfo.value, "__suppressed_exceptions__", ())
    assert len(suppressed) == 1
    assert isinstance(suppressed[0], RuntimeError)


@pytest.mark.asyncio
async def test_dispatch_signal_cancellation_ends_dispatch(app):
    started = asyncio.Event()
    ran_after = []
    reported = []

    @app.signal(Event.SERVER_EXCEPTION_REPORT)
    async def report(exception: Exception):
        reported.append(exception)

    @app.signal("foo.bar.baz")
    async def first(**_):
        started.set()
        await asyncio.sleep(3600)

    @app.signal("foo.bar.baz")
    async def second(**_):
        ran_after.append(True)

    app.signal_router.finalize()

    dispatch = asyncio.create_task(app.dispatch("foo.bar.baz", inline=True))
    await started.wait()
    dispatch.cancel()

    with pytest.raises(asyncio.CancelledError):
        await dispatch

    await asyncio.sleep(0)

    # Cancellation ends the current dispatch immediately: the remaining
    # handlers never run and it is not reported as a handler failure.
    assert not ran_after
    assert not reported


@pytest.mark.asyncio
async def test_repeated_dispatch_does_not_retrigger_resolved_waiter(app):
    handler_runs = 0

    @app.signal("foo.bar.baz")
    async def handler(**_):
        nonlocal handler_runs
        handler_runs += 1

    app.signal_router.finalize()

    waiter = asyncio.create_task(app.event("foo.bar.baz"))
    await asyncio.sleep(0)

    # The inline dispatches do not yield to the waiter task, so after the
    # first one the resolved waiter is still queued. It must be resolved
    # once instead of being triggered again (which previously raised
    # InvalidStateError and aborted the second dispatch).
    await app.dispatch("foo.bar.baz", inline=True)
    await app.dispatch("foo.bar.baz", inline=True)

    assert await waiter == {}
    assert handler_runs == 2


@pytest.mark.asyncio
async def test_dispatch_with_cancelled_waiter_does_not_raise(app):
    handler_runs = 0

    @app.signal("foo.bar.baz")
    async def handler(**_):
        nonlocal handler_runs
        handler_runs += 1

    app.signal_router.finalize()

    waiter = asyncio.create_task(app.event("foo.bar.baz"))
    await asyncio.sleep(0)

    # Put the queued waiter into a cancelled terminal state before its
    # waiting task has removed it.
    signal = app.signal_router.name_index["foo.bar.baz"]
    signal.ctx.waiters[0].future.cancel()

    # The stale, cancelled waiter is ignored instead of aborting the
    # dispatch with InvalidStateError.
    await app.dispatch("foo.bar.baz", inline=True)

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert handler_runs == 1


@pytest.mark.asyncio
async def test_waiter_can_wait_again_after_dispatch(app):
    @app.signal("foo.bar.baz")
    async def handler(**_): ...

    app.signal_router.finalize()

    for context in ({}, {"amount": 1}):
        waiter = asyncio.create_task(app.event("foo.bar.baz"))
        await asyncio.sleep(0)
        await app.dispatch("foo.bar.baz", inline=True, context=dict(context))
        assert await waiter == context


@pytest.mark.asyncio
async def test_dispatch_signal_reverse_continues_after_failure(app):
    # Mirrors the shutdown lifecycle, which dispatches in reverse: a
    # failing cleanup listener must not prevent the remaining cleanup
    # listeners from running, while the failure still propagates.
    calls = []

    @app.signal("foo.bar.baz", priority=0)
    async def failing(**_):
        calls.append("failing")
        raise ValueError("cleanup failed")

    @app.signal("foo.bar.baz", priority=1)
    async def cleanup(**_):
        calls.append("cleanup")

    app.signal_router.finalize()

    with pytest.raises(ValueError, match="cleanup failed"):
        await app.dispatch("foo.bar.baz", inline=True, reverse=True)

    assert calls == ["failing", "cleanup"]
