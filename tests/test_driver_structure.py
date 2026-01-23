# -*- coding: UTF-8 -*-
from __future__ import annotations

import ast
import pathlib
import unittest


class DriverStructureTests(unittest.TestCase):
	def test_cancel_is_class_method_not_nested_in_speak(self) -> None:
		repo_root = pathlib.Path(__file__).resolve().parents[1]
		driver_path = repo_root / "addon" / "synthDrivers" / "apollo2" / "driver.py"
		module = ast.parse(driver_path.read_text(encoding="utf-8"))

		synth_class = None
		for node in module.body:
			if isinstance(node, ast.ClassDef) and node.name == "SynthDriver":
				synth_class = node
				break
		self.assertIsNotNone(synth_class, "SynthDriver class not found in driver.py")

		methods = [n for n in synth_class.body if isinstance(n, ast.FunctionDef)]
		method_names = {m.name for m in methods}
		self.assertIn("speak", method_names)
		self.assertIn("cancel", method_names)

		speak_method = next(m for m in methods if m.name == "speak")
		nested_def_names = {n.name for n in speak_method.body if isinstance(n, ast.FunctionDef)}
		self.assertNotIn("cancel", nested_def_names)


if __name__ == "__main__":
	unittest.main()

