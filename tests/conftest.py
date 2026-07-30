from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _allow_fixture_seeding_of_governed_facts(request: pytest.FixtureRequest):
    """Let tests seed governed facts directly, without weakening production.

    `active_workout_plan` is a derived mirror: production may only write it
    inside `user_model.authorize_governed_fact_write`, and an unauthorized
    write raises. Sixteen test files legitimately seed that fact to build a
    world, and requiring each to wrap an authorization would add ceremony
    without adding safety -- a fixture seeding state is not the drift the guard
    exists to catch.

    Deliberately NOT implemented by teaching the assertion to detect pytest:
    that would make the guard unable to fire in its own tests, which is
    precisely where it must fire.

    Tests that assert the refusal opt out with `@pytest.mark.governed_facts`,
    so the assertion is live exactly where it is under examination.

    Implemented by relaxing the TASK binding rather than by opening an
    authorization here. A sync fixture and an async test body run in different
    asyncio tasks -- pytest-asyncio creates one per test -- so an authorization
    opened in this fixture would be *inherited* by the test's task and refused
    by the very rule that stops a background write from outliving its owner.
    Relaxing the binding keeps that production rule intact while letting test
    setup seed freely.
    """
    import user_model

    if request.node.get_closest_marker("governed_facts"):
        yield
        return

    with user_model.authorize_governed_fact_write(
        "test fixture seeding", bind_to_task=False
    ):
        yield


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "governed_facts: run with governed-fact write authorization DISABLED, "
        "so the runtime assertion is live and can be asserted against.",
    )
