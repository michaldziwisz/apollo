# -*- coding: UTF-8 -*-
from __future__ import annotations


def decode_swapped_hex_byte(two_ascii_hex_digits: bytes) -> int:
	"""
	Dolphin uses "low nibble first" ASCII hex in some responses, e.g. b"40" => 0x04.
	"""
	if len(two_ascii_hex_digits) != 2:
		raise ValueError("Expected 2 ASCII hex digits")
	normalized = bytes((two_ascii_hex_digits[1], two_ascii_hex_digits[0])).upper()
	return int(normalized.decode("ascii"), 16)


def decode_index_counter(two_ascii_hex_digits: bytes, pending_count: int) -> int:
	"""
	Apollo firmware variants disagree on hex digit order for the index counter.
	Try both and prefer a value that fits the number of pending marks.
	"""
	if len(two_ascii_hex_digits) != 2:
		raise ValueError("Expected 2 ASCII hex digits")
	try:
		normal = int(two_ascii_hex_digits.decode("ascii"), 16)
	except Exception:
		normal = 0
	try:
		swapped = decode_swapped_hex_byte(two_ascii_hex_digits)
	except Exception:
		swapped = normal

	candidates_in_range = [v for v in (normal, swapped) if 0 <= v <= pending_count]
	if candidates_in_range:
		# Prefer the larger value to avoid popping indexes too early.
		return max(candidates_in_range)
	return min(normal, swapped)

