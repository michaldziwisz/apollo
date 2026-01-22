# -*- coding: UTF-8 -*-
from __future__ import annotations

import unittest

from apollo2 import protocol


class ProtocolTests(unittest.TestCase):
	def test_control_bytes(self) -> None:
		self.assertEqual(protocol.CR, b"\r")
		self.assertEqual(protocol.NAK, b"\x15")
		self.assertEqual(protocol.MUTE, b"\x18")

	def test_indexing_commands(self) -> None:
		self.assertEqual(protocol.INDEX_QUERY_COMMAND, b"@I?")
		self.assertEqual(protocol.INDEX_ENABLE_COMMAND, b"@I+ ")
		self.assertEqual(protocol.INDEX_MARK_COMMAND, b"@I+ ")
		self.assertNotEqual(protocol.INDEX_QUERY_COMMAND, b"@1?")


if __name__ == "__main__":
	unittest.main()

