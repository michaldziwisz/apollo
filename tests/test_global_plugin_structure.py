# -*- coding: UTF-8 -*-
from __future__ import annotations

import ast
import pathlib
import unittest


class GlobalPluginStructureTests(unittest.TestCase):
	def _module(self) -> ast.Module:
		repo_root = pathlib.Path(__file__).resolve().parents[1]
		plugin_path = repo_root / "addon" / "globalPlugins" / "apollo2.py"
		return ast.parse(plugin_path.read_text(encoding="utf-8"))

	def test_disable_braille_has_no_default_nvda_shift_b_gesture(self) -> None:
		repo_root = pathlib.Path(__file__).resolve().parents[1]
		plugin_path = repo_root / "addon" / "globalPlugins" / "apollo2.py"
		source = plugin_path.read_text(encoding="utf-8").lower()
		self.assertNotIn("kb:nvda+shift+b", source)

	def test_disable_braille_script_is_exposed_for_manual_assignment(self) -> None:
		module = self._module()
		for node in ast.walk(module):
			if isinstance(node, ast.FunctionDef) and node.name == "script_disableBrailleAutoDetect":
				for decorator in node.decorator_list:
					if not isinstance(decorator, ast.Call):
						continue
					if isinstance(decorator.func, ast.Name) and decorator.func.id == "script":
						kw_names = {kw.arg for kw in decorator.keywords}
						self.assertIn("description", kw_names)
						self.assertNotIn("gesture", kw_names)
						return
		self.fail("script_disableBrailleAutoDetect is not exposed via @script without a default gesture")


if __name__ == "__main__":
	unittest.main()
