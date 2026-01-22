# -*- coding: UTF-8 -*-
from __future__ import annotations

import unittest

from apollo2.formants import (
	FORMANT_DELTA_HARD_MAX,
	FORMANT_DELTA_HARD_MIN,
	get_formant_adjust_commands,
	get_formant_commands_from_deltas,
	get_formant_diff_commands,
)


class FormantsTests(unittest.TestCase):
	def test_constants(self) -> None:
		self.assertEqual(FORMANT_DELTA_HARD_MIN, -255)
		self.assertEqual(FORMANT_DELTA_HARD_MAX, 255)

	def test_get_formant_commands_from_deltas(self) -> None:
		self.assertEqual(get_formant_commands_from_deltas([0, 0, 0]), [])
		self.assertEqual(get_formant_commands_from_deltas([1]), ["@u001+ "])
		self.assertEqual(get_formant_commands_from_deltas([-1]), ["@u001- "])
		# Clamp to full 1-byte delta range.
		self.assertEqual(get_formant_commands_from_deltas([999]), ["@u0FF+ "])
		self.assertEqual(get_formant_commands_from_deltas([-999]), ["@u0FF- "])

	def test_get_formant_adjust_commands_chunks_large_diffs(self) -> None:
		self.assertEqual(get_formant_adjust_commands(0, 0), [])
		self.assertEqual(get_formant_adjust_commands(0, 10), ["@u00A+ "])
		self.assertEqual(get_formant_adjust_commands(0, -10), ["@u00A- "])
		self.assertEqual(get_formant_adjust_commands(0, 300), ["@u0FF+ ", "@u02D+ "])
		self.assertEqual(get_formant_adjust_commands(0, -300), ["@u0FF- ", "@u02D- "])

	def test_get_formant_diff_commands_compares_to_applied(self) -> None:
		self.assertEqual(get_formant_diff_commands([10], [0]), ["@u00A+ "])
		self.assertEqual(get_formant_diff_commands([0], [10]), ["@u00A- "])
		# Handle missing applied values (treat as 0).
		self.assertEqual(get_formant_diff_commands([0, 1], [0]), ["@u101+ "])


if __name__ == "__main__":
	unittest.main()

