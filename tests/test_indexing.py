# -*- coding: UTF-8 -*-
from __future__ import annotations

import unittest

from apollo2.indexing import decode_index_counter, decode_swapped_hex_byte


class IndexingTests(unittest.TestCase):
	def test_decode_swapped_hex_byte(self) -> None:
		self.assertEqual(decode_swapped_hex_byte(b"40"), 0x04)
		self.assertEqual(decode_swapped_hex_byte(b"0F"), 0xF0)

	def test_decode_swapped_hex_byte_requires_two_digits(self) -> None:
		with self.assertRaises(ValueError):
			decode_swapped_hex_byte(b"")
		with self.assertRaises(ValueError):
			decode_swapped_hex_byte(b"0")
		with self.assertRaises(ValueError):
			decode_swapped_hex_byte(b"000")

	def test_decode_index_counter_prefers_candidate_within_range(self) -> None:
		# Normal would be 0x40=64; swapped is 0x04=4. With only 10 pending, pick 4.
		self.assertEqual(decode_index_counter(b"40", pending_count=10), 4)
		# Normal is 5; swapped is 0x50=80. With only 10 pending, pick 5.
		self.assertEqual(decode_index_counter(b"05", pending_count=10), 5)

	def test_decode_index_counter_prefers_larger_candidate_in_range(self) -> None:
		# Both 0x12=18 and swapped 0x21=33 fit; prefer 33 to avoid popping indexes too early.
		self.assertEqual(decode_index_counter(b"12", pending_count=40), 0x21)

	def test_decode_index_counter_fallbacks_when_out_of_range(self) -> None:
		# Normal is 0xF0=240; swapped is 0x0F=15. With 100 pending, pick 15.
		self.assertEqual(decode_index_counter(b"F0", pending_count=100), 15)
		self.assertEqual(decode_index_counter(b"0F", pending_count=20), 15)

	def test_decode_index_counter_requires_two_digits(self) -> None:
		with self.assertRaises(ValueError):
			decode_index_counter(b"F", pending_count=0)


if __name__ == "__main__":
	unittest.main()

