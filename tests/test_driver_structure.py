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

	def test_speak_can_ignore_delayed_character_description_break(self) -> None:
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
		speak_method = next((m for m in methods if m.name == "speak"), None)
		self.assertIsNotNone(speak_method, "speak method not found in SynthDriver")

		def _contains_self_attr(node: ast.AST, attr_name: str) -> bool:
			for n in ast.walk(node):
				if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
					if n.attr == attr_name:
						return True
			return False

		def _contains_name(node: ast.AST, name: str) -> bool:
			for n in ast.walk(node):
				if isinstance(n, ast.Name) and n.id == name:
					return True
			return False

		def _contains_continue(node: ast.AST) -> bool:
			return any(isinstance(n, ast.Continue) for n in ast.walk(node))

		found_guard = False
		for n in ast.walk(speak_method):
			if not isinstance(n, ast.If):
				continue
			if not _contains_continue(n):
				continue
			if not _contains_self_attr(n.test, "_ignoreDelayedCharacterDescriptionPause"):
				continue
			if not _contains_name(n.test, "_DELAYED_CHARACTER_DESCRIPTION_BREAK_MS"):
				continue
			found_guard = True
			break

		self.assertTrue(
			found_guard,
			"speak() does not include a guard to ignore NVDA's delayed character-description BreakCommand.",
		)

	def test_settings_prefix_does_not_force_voice_filter_unconditionally(self) -> None:
		"""Ensure `@$` is not appended unconditionally in _settingsPrefix.

		Some ROM/firmware combinations appear to treat `@$` as overriding preset voices. If it is
		always sent (e.g. in "auto" mode), changing NVDA's Voice may have little to no audible effect.
		"""
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
		settings_prefix = next((m for m in methods if m.name == "_settingsPrefix"), None)
		self.assertIsNotNone(settings_prefix, "_settingsPrefix method not found in SynthDriver")

		def _is_unconditional_at_dollar_append(stmt: ast.stmt) -> bool:
			if not isinstance(stmt, ast.Expr):
				return False
			call = stmt.value
			if not isinstance(call, ast.Call):
				return False
			func = call.func
			if not (isinstance(func, ast.Attribute) and func.attr == "append"):
				return False
			if not (isinstance(func.value, ast.Name) and func.value.id == "commands"):
				return False
			if not call.args:
				return False
			for n in ast.walk(call.args[0]):
				if isinstance(n, ast.Constant) and isinstance(n.value, str) and "@$" in n.value:
					return True
			return False

		unconditional = [stmt for stmt in settings_prefix.body if _is_unconditional_at_dollar_append(stmt)]
		self.assertFalse(
			unconditional,
			"_settingsPrefix appends '@$' unconditionally; it should be conditional on an explicit voiceFilter.",
		)


if __name__ == "__main__":
	unittest.main()
