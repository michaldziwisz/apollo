# -*- coding: UTF-8 -*-
from __future__ import annotations

from . import numbers_pl


POLISH_TO_APOLLO_TRANSLATION = bytes.maketrans(
	bytes(
		[
			0xB9,  # ą
			0xE6,  # ć
			0xEA,  # ę
			0xB3,  # ł
			0xF1,  # ń
			0xF3,  # ó
			0x9C,  # ś
			0x9F,  # ź
			0xBF,  # ż
			0xA5,  # Ą
			0xC6,  # Ć
			0xCA,  # Ę
			0xA3,  # Ł
			0xD1,  # Ń
			0xD3,  # Ó
			0x8C,  # Ś
			0x8F,  # Ź
			0xAF,  # Ż
		],
	),
	bytes(
		[
			0x86,  # ą
			0x8D,  # ć
			0x91,  # ę
			0x92,  # ł
			0xA4,  # ń
			0xA2,  # ó
			0x9E,  # ś
			0xA6,  # ź
			0xA7,  # ż
			0x8F,  # Ą
			0x95,  # Ć
			0x90,  # Ę
			0x9C,  # Ł
			0xA5,  # Ń
			0xA3,  # Ó
			0x98,  # Ś
			0xA0,  # Ź
			0xA1,  # Ż
		],
	),
)


def sanitize_text(text: str) -> str:
	if not text:
		return ""
	# Apollo uses @-prefixed commands; don't allow those to leak from NVDA text.
	text = text.replace("@", " ")
	# Normalize all whitespace/control chars to ASCII space to avoid word-join bugs
	# (e.g. tabs / non-breaking spaces not treated as separators by the synth).
	return "".join(
		" " if (ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F) else ch for ch in text
	)


def encode_text(text: str, *, expand_numbers: bool = True) -> bytes:
	if expand_numbers and any("0" <= ch <= "9" for ch in text):
		text = numbers_pl.dajNapisZLiczbamiWPostaciSlownej(text)
	cp1250 = text.encode("cp1250", "replace")
	return cp1250.translate(POLISH_TO_APOLLO_TRANSLATION)

