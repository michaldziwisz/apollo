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

	def test_write_loop_writes_when_serial_already_connected(self) -> None:
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
		write_loop = next((m for m in methods if m.name == "_writeLoop"), None)
		self.assertIsNotNone(write_loop, "_writeLoop method not found in SynthDriver")

		def _contains_call(node: ast.AST, name: str) -> bool:
			for n in ast.walk(node):
				if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name:
					return True
			return False

		# Locate the main `while True:` loop in `_writeLoop`.
		while_node = next((n for n in write_loop.body if isinstance(n, ast.While)), None)
		self.assertIsNotNone(while_node, "No while-loop found in _writeLoop")

		# Find the `if ser is None:` statement and ensure there is a `writeBytes(...)` call
		# after it at the same nesting level (i.e., not exclusively inside the ser-is-None branch).
		ser_none_if_indexes = []
		for i, stmt in enumerate(while_node.body):
			if not isinstance(stmt, ast.If):
				continue
			# Matches: `if ser is None:`
			test = stmt.test
			if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "ser":
				if len(test.ops) == 1 and isinstance(test.ops[0], ast.Is) and len(test.comparators) == 1:
					comp = test.comparators[0]
					if isinstance(comp, ast.Constant) and comp.value is None:
						ser_none_if_indexes.append(i)

		self.assertTrue(ser_none_if_indexes, "No 'if ser is None' block found in _writeLoop")

		found_write_after = False
		for idx in ser_none_if_indexes:
			for stmt in while_node.body[idx + 1 :]:
				if _contains_call(stmt, "writeBytes"):
					found_write_after = True
					break
			if found_write_after:
				break

		self.assertTrue(
			found_write_after,
			"_writeLoop never calls writeBytes outside the 'if ser is None' block; this breaks speech output.",
		)


if __name__ == "__main__":
	unittest.main()
