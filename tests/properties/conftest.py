"""Shared fixture for the generator-coverage tests in this directory.

Hypothesis (6.x, "local constants") mines literal constants from every
non-test module in ``sys.modules`` whose file lives in the project, and mixes
them into about 5% of primitive draws. Which ``taperescue`` modules are
imported depends on which test files pytest collected in the same process
(for example ``tests/integration`` loads ``taperescue.decoder_adapter.run``
and ``taperescue.fixtures.fsk``). A different constant pool changes the
values drawn, so a ``derandomize=True`` sample is only fixed for a fixed set
of loaded modules.

``fixed_constant_pool`` empties that local pool while a coverage sample is
drawn, so the sample depends only on the strategy and the derandomize seed.
Hypothesis's built-in global constants are still used. The property tests
themselves are not affected: the pool and its cache are restored afterwards.

This touches Hypothesis internals (``providers._get_local_constants`` and
``providers.CONSTANTS_CACHE``). On a Hypothesis without local constants
(``HypothesisProvider`` has no ``_local_constants``) it does nothing. If a
version has local constants but renames those names, the fixture fails with
an error instead of silently drawing an unpinned sample.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def fixed_constant_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from hypothesis.internal.conjecture import providers

    if not hasattr(providers.HypothesisProvider, "_local_constants"):
        # Older Hypothesis without local constants: the sample is already fixed.
        yield
        return

    from hypothesis.internal.constants_ast import Constants

    for name in ("_get_local_constants", "CONSTANTS_CACHE"):
        if not hasattr(providers, name):
            pytest.fail(
                f"hypothesis.internal.conjecture.providers.{name} not found; "
                "update tests/properties/conftest.py for this Hypothesis version"
            )

    empty = Constants()
    # The cache holds permitted-constant tuples built from the current pool.
    providers.CONSTANTS_CACHE.cache.clear()
    monkeypatch.setattr(providers, "_get_local_constants", lambda: empty)
    try:
        yield
    finally:
        monkeypatch.undo()
        providers.CONSTANTS_CACHE.cache.clear()
