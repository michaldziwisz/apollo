# -*- coding: UTF-8 -*-
"""
Entry point for NVDA's `synthDrivers.apollo2` module.

This package also contains helper modules that we want to be able to import in CI for unit tests.
NVDA-only modules (addonHandler, synthDriverHandler, etc.) are therefore imported lazily:

- Under NVDA: `synthDriverHandler` is available and we import `SynthDriver` from `.driver`.
- Outside NVDA (CI/unit tests): we avoid importing `.driver` so tests can import helpers safely.
"""

from __future__ import annotations

try:
	import synthDriverHandler  # type: ignore[import-not-found]  # noqa: F401
except ImportError:
	# Not running under NVDA (e.g. unit tests).
	pass
else:
	from .driver import SynthDriver  # noqa: F401

