# -*- coding: UTF-8 -*-
"""Guard the agreement between the synth driver and the global plugin about device probing.

The driver's auto-detection identifies an Apollo with the harmless "@V?" setting query, while
release 0.1.87 had moved detection to the indexing query because some ROMs never answer "@V?".
Both probes must therefore stay available on BOTH sides:

- the driver must fall back to the indexing handshake when "@V?" stays silent, otherwise the
  faster scan can lose a device that older releases used to find;
- the global plugin's "test connection" command must accept both replies too, otherwise it can
  report a failure for a device the driver connects to (or the other way round).

These tests parse the sources, so they run without NVDA installed.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


def _read(*parts: str) -> str:
	repo_root = pathlib.Path(__file__).resolve().parents[1]
	return repo_root.joinpath(*parts).read_text(encoding="utf-8")


def _called_function_names(node: ast.AST) -> set[str]:
	"""Names of functions actually CALLED inside ``node`` (not merely defined nearby)."""
	names: set[str] = set()
	for child in ast.walk(node):
		if isinstance(child, ast.Call):
			func = child.func
			if isinstance(func, ast.Name):
				names.add(func.id)
			elif isinstance(func, ast.Attribute):
				names.add(func.attr)
	return names


def _function(source: str, name: str) -> ast.FunctionDef:
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.FunctionDef) and node.name == name:
			return node
	raise AssertionError(f"function {name} not found")


class ProbeAgreementTests(unittest.TestCase):
	def test_driver_auto_detect_uses_both_probes(self) -> None:
		source = _read("addon", "synthDrivers", "apollo2", "driver.py")
		self.assertIn(
			'probeSettingResponseDirect(ser, command=b"@V?"',
			source,
			"The driver no longer identifies the device with the @V? setting query.",
		)
		self.assertIn(
			"ensureIndexingAndProbe",
			source,
			"The driver no longer performs the indexing handshake at all.",
		)
		# The indexing handshake must be reachable for ports that stayed silent on @V?,
		# not only for ports already confirmed by @V?.
		self.assertIn(
			"answered neither @V? nor the indexing query",
			source,
			"The driver's auto-detection has no indexing fallback for ports that ignore @V?; "
			"ROMs that only answer the indexing query would stop being detected (see 0.1.87).",
		)

	def test_global_plugin_connection_test_accepts_both_probes(self) -> None:
		source = _read("addon", "globalPlugins", "apollo2.py")
		self.assertIn(
			'_SETTING_QUERY_COMMAND = b"@V?"',
			source,
			"The connection test uses a different setting query than the driver.",
		)
		# Both probes must actually be CALLED by the connection test, not just defined in the
		# module: a probe that exists but is never reached cannot agree with the driver.
		called = _called_function_names(_function(source, "_testApolloConnection"))
		self.assertIn(
			"_probeApolloSettingResponse",
			called,
			"_testApolloConnection never calls the @V? probe, so it can report a failure for a "
			"device the driver detects via @V?.",
		)
		self.assertIn(
			"_probeApolloIndexResponse",
			called,
			"_testApolloConnection never calls the indexing probe, so ROMs that only answer the "
			"indexing query would fail the connection test.",
		)

	def test_both_sides_use_the_same_setting_query(self) -> None:
		driver = _read("addon", "synthDrivers", "apollo2", "driver.py")
		plugin = _read("addon", "globalPlugins", "apollo2.py")
		self.assertIn('b"@V?"', driver)
		self.assertIn('b"@V?"', plugin)
		# Response prefixes must match as well; "@V?" replies with "Vhh".
		self.assertIn('expectedPrefix=b"V"', driver)
		self.assertIn('_SETTING_RESPONSE_PREFIX = b"V"', plugin)


if __name__ == "__main__":
	unittest.main()
