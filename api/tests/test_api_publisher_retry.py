"""The queue publisher reconnects once when the broker dropped its connection.

No database and no broker: `_send` (the one call that reaches Celery) is replaced, so what is tested
is the policy around it. That the reset really makes kombu open a new connection after RabbitMQ
restarts is a claim about kombu's pools, checked by the worker runbook in docs/operations.md, not here.
"""

import uuid

import kombu.pools
import pytest
from kombu.exceptions import OperationalError
from repolace_api.publisher import CeleryTaskPublisher

pytestmark = pytest.mark.anyio

BROKER = "amqp://nobody:nobody@127.0.0.1:1//"


def publisher_failing_with(monkeypatch, *errors: Exception | None):
    """A publisher whose successive sends raise `errors` in turn (None means the send succeeds)."""
    publisher = CeleryTaskPublisher(BROKER)
    outcomes = list(errors)
    sent: list[uuid.UUID] = []
    resets: list[bool] = []

    def send(task_id):
        error = outcomes.pop(0)
        if error is not None:
            raise error
        sent.append(task_id)

    monkeypatch.setattr(publisher, "_send", send)
    monkeypatch.setattr(kombu.pools, "reset", lambda: resets.append(True))
    return publisher, sent, resets


@pytest.mark.parametrize(
    "error", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError(), OperationalError("[Errno 32] Broken pipe")]
)
async def test_a_dropped_connection_is_replaced_and_the_publish_tried_again(monkeypatch, error):
    publisher, sent, resets = publisher_failing_with(monkeypatch, error, None)
    first_app = publisher._app
    task_id = uuid.uuid4()

    assert await publisher.publish(task_id) is True
    assert sent == [task_id]
    assert resets == [True]
    assert publisher._app is not first_app
    assert publisher._app.conf.broker_connection_timeout == 3
    assert publisher._app.conf.broker_connection_retry is False


async def test_a_broker_that_is_still_down_fails_after_one_retry_without_raising(monkeypatch):
    publisher, sent, resets = publisher_failing_with(monkeypatch, ConnectionRefusedError(), ConnectionRefusedError())

    assert await publisher.publish(uuid.uuid4()) is False
    assert sent == []
    assert resets == [True]  # one reconnect, not a loop


async def test_an_error_that_is_not_a_connection_error_is_not_retried(monkeypatch):
    publisher, sent, resets = publisher_failing_with(monkeypatch, ValueError("bad message"), None)

    assert await publisher.publish(uuid.uuid4()) is False
    assert sent == []
    assert resets == []


async def test_a_working_connection_is_left_alone(monkeypatch):
    publisher, sent, resets = publisher_failing_with(monkeypatch, None)
    app = publisher._app
    task_id = uuid.uuid4()

    assert await publisher.publish(task_id) is True
    assert sent == [task_id]
    assert resets == []
    assert publisher._app is app
