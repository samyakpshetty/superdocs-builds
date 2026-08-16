"""Suite-wide setup.

The fake reads ``FAKE_STATE_DIR`` so that the API container and the worker container share
one SuperDocs, the way both would share a real one; compose sets it for both services. A test
must not inherit that. It runs in the same image, so the variable is present, and the suite
would quietly start sharing sessions and jobs between test cases through a file on a volume —
where a test's result depends on what ran before it, and on what a previous *run* left
behind.

Cleared once, here, so the tests are hermetic wherever they run: inside compose, in CI, or on
a developer's machine that happens to export it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_fake_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets a fake that remembers nothing but what the test told it."""
    monkeypatch.delenv("FAKE_STATE_DIR", raising=False)
