"""Compatibility bridge for code extracted from the historical facade.

The project used to keep application globals in ``coach_bot``. During the
modular rebuild, public functions moved to focused modules while the facade
remains patchable by existing integrations and tests. The decorator refreshes
only referenced globals immediately before each call.
"""
from __future__ import annotations

import functools
import inspect
import sys
from collections.abc import Callable, Iterable
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])

def _sync(function: Callable[..., Any], names: Iterable[str]) -> None:
    facade = sys.modules.get("coach_bot")
    if facade is None:
        return
    namespace = function.__globals__
    for name in names:
        if hasattr(facade, name):
            namespace[name] = getattr(facade, name)

def runtime_bound(names: Iterable[str]) -> Callable[[F], F]:
    """Refresh legacy facade dependencies before invoking an extracted callable."""
    names = tuple(names)
    def decorate(function: F) -> F:
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _sync(function, names)
                return await function(*args, **kwargs)
            return cast(F, async_wrapper)
        @functools.wraps(function)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            _sync(function, names)
            return function(*args, **kwargs)
        return cast(F, sync_wrapper)
    return decorate
