"""`scrub_host_paths`: task error text is shown without the worker's filesystem paths."""

import pytest
from repolace_api.ratelimit import SlidingWindowLimiter
from repolace_api.redact import scrub_host_paths


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "git clone https://github.com/acme/x.git /tmp/repolace-task-kor7yewp/repo failed (128)",
            "git clone https://github.com/acme/x.git <path>/repo failed (128)",
        ),
        ('File "/home/alice/Work/repolace/.venv/lib/python3.12/x.py", line 3', 'File "<path>/x.py", line 3'),
        ("/var/tmp/repolace-workspaces/repolace-task-ab12/repo/src/pkg/mod.py:10: error", "<checkout>/src/pkg/mod.py:10: error"),
        ("[Errno 13] Permission denied: '/run/user/1000/docker.sock'", "[Errno 13] Permission denied: '<path>/docker.sock'"),
        ("ratio 1/2, relative a/b/c, https://x.example/a/b and file:///etc/passwd", "ratio 1/2, relative a/b/c, https://x.example/a/b and file:///etc/passwd"),
        ("a single segment /usr stays", "a single segment /usr stays"),
    ],
)
def test_host_paths_are_replaced_and_urls_kept(text, expected):
    assert scrub_host_paths(text) == expected


@pytest.mark.parametrize("value", [None, ""])
def test_empty_values_pass_through(value):
    assert scrub_host_paths(value) == value


def test_retry_after_counts_down_to_the_oldest_event():
    now = [100.0]
    limiter = SlidingWindowLimiter(limit=2, window=60, clock=lambda: now[0])
    assert limiter.retry_after("u") == 0
    assert limiter.allow("u") and limiter.allow("u") and not limiter.allow("u")
    now[0] = 130.0
    assert limiter.retry_after("u") == 31
    now[0] = 161.0
    assert limiter.allow("u") and limiter.retry_after("u") == 0
