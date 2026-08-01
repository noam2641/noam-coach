"""The runtime half of the active_workout_plan write governance.

The static guard in `test_workout_catalog_architecture_guards.py` freezes WHERE
a governed write may appear. It cannot prove what `set_fact(db, uid, key, ...)`
writes when the key is computed, and 27 such call sites exist across the fact
layer -- legitimate generic plumbing that would be absurd to rewrite to protect
one key.

So the static guard narrows the surface and this one actually holds it. A write
to a governed fact outside an explicit authorization raises, rather than being
logged and dropped: a silently-skipped mirror write leaves the fact stale while
the caller believes it succeeded, and nothing surfaces until a user is shown the
wrong plan.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import user_model
from db import Database
from helpers import utc_now


async def _db(tmp_path: Path) -> Database:
    db = Database(str(tmp_path / "governed.db"))
    await db.init()
    await db.execute(
        "INSERT INTO users(id, first_name, username, updated_at) VALUES(1,'A',NULL,?)",
        (utc_now(),),
    )
    return db


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
def test_the_governed_set_is_explicit_and_minimal() -> None:
    """A fact is governed because it MIRRORS an authoritative store.

    Pinned so the set cannot grow casually: adding a key here changes who is
    allowed to write it everywhere, which is an architecture decision.
    """
    assert user_model.GOVERNED_FACT_KEYS == frozenset({"active_workout_plan"})


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_an_unauthorized_governed_write_raises(tmp_path) -> None:
    db = await _db(tmp_path)

    with pytest.raises(PermissionError) as excinfo:
        await user_model.set_fact(
            db, 1, "active_workout_plan", {"sessions": []},
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )

    # The message has to tell the next engineer what to do instead, or they
    # will reach for the shortcut the guard exists to prevent.
    message = str(excinfo.value)
    assert "derived mirror" in message
    assert "authorize_governed_fact_write" in message

    # And nothing was written -- the refusal is not partial.
    assert await user_model.get_fact(db, 1, "active_workout_plan") is None


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_an_authorized_governed_write_succeeds(tmp_path) -> None:
    db = await _db(tmp_path)

    with user_model.authorize_governed_fact_write("test owner"):
        await user_model.set_fact(
            db, 1, "active_workout_plan", {"sessions": [{"code": "A"}]},
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )

    fact = await user_model.get_fact(db, 1, "active_workout_plan")
    assert fact is not None
    assert fact["value"]["sessions"][0]["code"] == "A"


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_authorization_does_not_leak_past_its_block(tmp_path) -> None:
    """The window closes even on the happy path.

    A leaked authorization is worse than none: it would make every later write
    in the same task look authorized, and the guard would pass while protecting
    nothing.
    """
    db = await _db(tmp_path)

    with user_model.authorize_governed_fact_write("test owner"):
        await user_model.set_fact(
            db, 1, "active_workout_plan", {"sessions": []},
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )

    with pytest.raises(PermissionError):
        await user_model.set_fact(
            db, 1, "active_workout_plan", {"sessions": [{"code": "B"}]},
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_authorization_is_released_after_an_exception(tmp_path) -> None:
    """An error inside the block must not leave the door open."""
    db = await _db(tmp_path)

    with pytest.raises(RuntimeError):
        with user_model.authorize_governed_fact_write("test owner"):
            raise RuntimeError("boom")

    with pytest.raises(PermissionError):
        await user_model.set_fact(
            db, 1, "active_workout_plan", {"sessions": []},
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_ungoverned_facts_are_completely_unaffected(tmp_path) -> None:
    """The blast radius is one key.

    The fact layer has hundreds of writers; if this guard touched them the cure
    would be worse than the disease.
    """
    db = await _db(tmp_path)

    await user_model.set_fact(
        db, 1, "training_limitations", {"location": "elbow"},
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
    )
    await user_model.set_fact(
        db, 1, "weight_kg", 82.5,
        kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
    )

    assert await user_model.get_fact(db, 1, "training_limitations") is not None
    assert await user_model.get_fact(db, 1, "weight_kg") is not None


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_a_dynamic_key_is_caught_at_runtime(tmp_path) -> None:
    """The exact bypass the static guard cannot see.

    `key = "active_workout_plan"` then `set_fact(db, uid, key, ...)` matches no
    pattern and parses to no literal. This is why the runtime half exists, and
    it is the single most important test in this file.
    """
    db = await _db(tmp_path)
    key = "".join(["active", "_workout", "_plan"])  # defeats any literal scan

    with pytest.raises(PermissionError):
        await user_model.set_fact(
            db, 1, key, {"sessions": []},
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )


# ---------------------------------------------------------------------------
# The legitimate owners still work
# ---------------------------------------------------------------------------
def test_the_sole_owner_authorizes_its_mirror_write() -> None:
    """Every governed write in production sits inside an authorization.

    A10 retired the second owner: `build_weekly_plan` no longer writes the
    mirror at all, so `planning.activate_plan` is the only writer left. One
    writer is what makes the activation gate enforceable -- a second one could
    always route around it.

    Asserted against source rather than by driving the flow: `activate_plan` is
    behind a readiness gate, so exercising it here would test that gate, not
    this one.
    What matters for A4 is the structural property -- a governed write is
    never reached without an owner claiming it -- and that is visible in the
    source and enforced at runtime by the tests above.
    """
    import ast
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    for relative in ("planning.py",):
        source = (root / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)

        authorized_ranges: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and getattr(call.func, "attr", getattr(call.func, "id", ""))
                    == "authorize_governed_fact_write"
                ):
                    authorized_ranges.append((node.lineno, node.end_lineno or node.lineno))

        governed_lines: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", getattr(node.func, "id", "")) != "set_fact":
                continue
            for arg in node.args[2:3]:
                if isinstance(arg, ast.Constant) and arg.value == "active_workout_plan":
                    governed_lines.append(node.lineno)

        assert governed_lines, f"{relative} should still contain a governed write"
        for line in governed_lines:
            assert any(
                start <= line <= end for start, end in authorized_ranges
            ), (
                f"{relative}:{line} writes active_workout_plan outside "
                "authorize_governed_fact_write -- it would raise at runtime"
            )


# ---------------------------------------------------------------------------
# Scope: the authorization must not outlive, or escape, the flow that opened it
# ---------------------------------------------------------------------------
@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_nested_authorization_restores_the_outer_one(tmp_path) -> None:
    """Exiting a nested block returns to the outer authorization, not to none.

    `ContextVar.reset(token)` restores the PREVIOUS value rather than clearing
    it. Pinning that here because the naive alternative -- setting None on exit
    -- would silently revoke an outer owner's authorization halfway through its
    own operation, and the failure would look like an unrelated permission bug.
    """
    db = await _db(tmp_path)

    with user_model.authorize_governed_fact_write("outer"):
        with user_model.authorize_governed_fact_write("inner"):
            await user_model.set_fact(
                db, 1, "active_workout_plan", {"sessions": [{"code": "inner"}]},
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            )
        # Still inside `outer` -- this must be permitted.
        await user_model.set_fact(
            db, 1, "active_workout_plan", {"sessions": [{"code": "outer"}]},
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )

    fact = await user_model.get_fact(db, 1, "active_workout_plan")
    assert fact["value"]["sessions"][0]["code"] == "outer"

    with pytest.raises(PermissionError):
        await user_model.set_fact(
            db, 1, "active_workout_plan", {"sessions": []},
            kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
        )


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_a_child_task_does_not_inherit_authorization(tmp_path) -> None:
    """The failure mode that motivated binding authorization to a task.

    `contextvars` are COPIED into a task at `asyncio.create_task`. Without the
    task check, an authorized owner could spawn a background task that inherits
    the authorization and performs a governed write LATER -- after the `with`
    block has exited and the owner has stopped being responsible for it.
    Measured on this codebase before the fix: the child write succeeded.

    A governed write must be made by the flow that claimed responsibility, so
    an inherited authorization is refused with its own distinct message.
    """
    db = await _db(tmp_path)
    result: dict[str, object] = {}

    async def _child() -> None:
        try:
            await user_model.set_fact(
                db, 1, "active_workout_plan", {"sessions": [{"code": "child"}]},
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            )
            result["outcome"] = "wrote"
        except PermissionError as exc:
            result["outcome"] = "refused"
            result["message"] = str(exc)

    with user_model.authorize_governed_fact_write("owner"):
        task = asyncio.create_task(_child())
    # The authorizing block has already exited by the time the task runs.
    await task

    assert result["outcome"] == "refused"
    assert "inherited" in str(result["message"])
    assert await user_model.get_fact(db, 1, "active_workout_plan") is None


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_a_task_may_open_its_own_authorization(tmp_path) -> None:
    """Refusing inheritance must not make background work impossible.

    A task that is genuinely an owner opens its own authorization and proceeds.
    Without this, the rule above would push callers toward disabling the guard
    rather than scoping it correctly.
    """
    db = await _db(tmp_path)

    async def _child() -> None:
        with user_model.authorize_governed_fact_write("child is the owner"):
            await user_model.set_fact(
                db, 1, "active_workout_plan", {"sessions": [{"code": "child"}]},
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            )

    await asyncio.create_task(_child())

    fact = await user_model.get_fact(db, 1, "active_workout_plan")
    assert fact["value"]["sessions"][0]["code"] == "child"


@pytest.mark.governed_facts
@pytest.mark.asyncio
async def test_concurrent_tasks_are_isolated(tmp_path) -> None:
    """One task authorizing must not authorize a sibling running beside it.

    Both tasks are alive at the same time, so this fails if authorization is
    stored anywhere process-wide instead of per-context.
    """
    db = await _db(tmp_path)
    outcomes: dict[str, str] = {}
    started = asyncio.Event()

    async def _authorized() -> None:
        with user_model.authorize_governed_fact_write("authorized sibling"):
            started.set()
            await asyncio.sleep(0.01)  # stay open while the other runs
            await user_model.set_fact(
                db, 1, "active_workout_plan", {"sessions": [{"code": "ok"}]},
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            )
            outcomes["authorized"] = "wrote"

    async def _unauthorized() -> None:
        await started.wait()  # overlap deliberately
        try:
            await user_model.set_fact(
                db, 1, "active_workout_plan", {"sessions": [{"code": "bad"}]},
                kind=user_model.KIND_FACT, source=user_model.SOURCE_USER,
            )
            outcomes["unauthorized"] = "wrote"
        except PermissionError:
            outcomes["unauthorized"] = "refused"

    await asyncio.gather(_authorized(), _unauthorized())

    assert outcomes == {"authorized": "wrote", "unauthorized": "refused"}
    fact = await user_model.get_fact(db, 1, "active_workout_plan")
    assert fact["value"]["sessions"][0]["code"] == "ok"


# ---------------------------------------------------------------------------
# The temporary owner is gone, and must stay gone
# ---------------------------------------------------------------------------
def test_build_weekly_plan_no_longer_writes_the_governed_fact() -> None:
    """A10 retired the temporary owner. This is the inverse of the test that
    guarded it while it existed.

    `build_weekly_plan` used to write `active_workout_plan` directly with no
    `plan_versions` row behind it -- the fact was the only copy, nothing could
    derive it, and the plan became active the instant it was built. That made
    an unconfirmed activation structurally unavoidable on that path: there was
    no candidate to hold and no activation call to gate.

    It now proposes a candidate through the canonical pipeline, so
    `planning.activate_plan` is once again the SOLE writer of the governed
    fact. Restoring a direct write here would restore pre-confirmation
    activation with it, which is why this is asserted at the source level
    rather than left to the allowlist alone.
    """
    import ast
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    source = (root / "noam_coach" / "bot" / "onboarding.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name != "set_fact":
            continue
        # set_fact(db, user_id, key, value, ...) -- key is third positional,
        # or a `key=` keyword.
        key = next(
            (kw.value for kw in node.keywords if kw.arg == "key"),
            node.args[2] if len(node.args) > 2 else None,
        )
        if isinstance(key, ast.Constant) and key.value in user_model.GOVERNED_FACT_KEYS:
            offenders.append(f"onboarding.py:{node.lineno} writes {key.value!r}")

    assert not offenders, (
        "onboarding.py must not write a governed fact again -- route it through "
        "planning.activate_plan, which is the only writer an activation gate "
        "can enforce: " + "; ".join(offenders)
    )
    assert "authorize_governed_fact_write" not in source, (
        "the temporary A4 authorization must be removed with the write it "
        "authorized, not left behind to re-enable one silently"
    )


def test_bind_to_task_escape_is_confined_to_test_setup() -> None:
    """`bind_to_task=False` must never appear in production code.

    It exists so a sync fixture can authorize an async test body -- two
    different asyncio tasks by construction. In production the same relaxation
    would re-open the exact hazard the binding closes: an owner spawning a
    background task that writes after the authorizing block has exited.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        if any(p in {"tests", "__pycache__", ".git", ".venv"} for p in parts):
            continue
        # user_model.py DEFINES the parameter; it is the one file that must
        # mention it. Everything else naming it is a caller.
        if path.name == "user_model.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "bind_to_task=False" in text:
            offenders.append(str(path.relative_to(root)).replace("\\", "/"))

    assert not offenders, (
        f"bind_to_task=False used in production code: {offenders}. It relaxes "
        "the task binding that stops an inherited authorization from writing "
        "after its owner has finished. Perform the write in the authorizing "
        "flow, or open a fresh authorization inside the owning task."
    )


def test_bind_to_task_defaults_to_true() -> None:
    """The safe behaviour must be what a caller gets without asking.

    A keyword-only parameter defaulting the other way would make every
    ordinary authorization inheritable, and nothing would look wrong at the
    call site.
    """
    import inspect

    signature = inspect.signature(user_model.authorize_governed_fact_write)
    parameter = signature.parameters["bind_to_task"]
    assert parameter.default is True
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
        "bind_to_task must be keyword-only so it can never be relaxed by a "
        "positional argument passed at the wrong index"
    )


def test_bind_to_task_escape_is_used_only_by_test_infrastructure() -> None:
    """Even within tests/, only fixtures and helpers may relax the binding.

    A test that relaxes it inline would be asserting against a weaker rule than
    production runs under, and would pass while the real contract regressed.
    """
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    allowed = {"tests/conftest.py"}
    offenders: list[str] = []
    for path in (root / "tests").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(root)).replace("\\", "/")
        if relative in allowed or relative == "tests/test_governed_fact_write_authorization.py":
            continue
        if "bind_to_task=False" in path.read_text(encoding="utf-8"):
            offenders.append(relative)

    assert not offenders, (
        f"bind_to_task=False used outside test infrastructure: {offenders}. "
        "Only the autouse fixture in conftest.py may relax the task binding; "
        "a test relaxing it inline is testing a weaker rule than production."
    )


def test_task_identity_uses_the_object_not_a_reusable_id() -> None:
    """Identity must survive garbage collection without aliasing.

    CPython reuses `id()` after an object is collected, so a finished task's id
    can later belong to an unrelated object. Storing the task itself and
    comparing with `is` makes a recycled number impossible to mistake for the
    authorizing task.
    """
    import ast
    import inspect

    source = inspect.getsource(user_model._current_task)
    tree = ast.parse(source.lstrip())
    # Strip the docstring: it EXPLAINS why id(task) is wrong, so a raw text
    # scan would flag the very comment documenting the rule.
    body = [n for n in tree.body[0].body if not (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    )]
    code = "\n".join(ast.unparse(n) for n in body)

    assert "return task" in code
    assert "id(task)" not in code, (
        "task identity must be the object, never id(task) -- ids are reused "
        "after collection, so a finished task's id can alias a new object"
    )
