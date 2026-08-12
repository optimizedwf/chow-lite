"""Runtime-registered plugin workflows (written by nine compose)."""
from __future__ import annotations

from collections.abc import Callable

from nine.chains.chain import Hop

PLUGIN_WORKFLOWS: dict[str, Callable[[], Hop]] = {}
