# -*- coding: UTF-8 -*-
from __future__ import annotations

import unittest

from apollo2.text import encode_text, sanitize_text


class TextTests(unittest.TestCase):
	def test_sanitize_text_removes_apollo_commands(self) -> None:
		self.assertEqual(sanitize_text("a@b"), "a b")

	def test_sanitize_text_normalizes_whitespace_and_controls(self) -> None:
		self.assertEqual(sanitize_text("a\tb\nc"), "a b c")
		self.assertEqual(sanitize_text("a\u00A0b"), "a b")
		self.assertEqual(sanitize_text("a\x00b\x7f"), "a b ")

	def test_encode_text_expands_numbers(self) -> None:
		self.assertEqual(encode_text("1"), b"jeden")
		expanded = encode_text("Za 2 dni")
		self.assertFalse(any(0x30 <= b <= 0x39 for b in expanded))

	def test_encode_text_can_skip_number_expansion(self) -> None:
		self.assertEqual(encode_text("1", expand_numbers=False), b"1")

	def test_encode_text_translates_polish_characters(self) -> None:
		# cp1250 "ą" (0xB9) is translated to Apollo encoding 0x86.
		self.assertEqual(encode_text("ą", expand_numbers=False), bytes([0x86]))


if __name__ == "__main__":
	unittest.main()

