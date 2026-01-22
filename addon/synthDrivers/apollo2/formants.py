# -*- coding: UTF-8 -*-
from __future__ import annotations

from typing import Sequence

FORMANT_DELTA_HARD_MIN = -255
FORMANT_DELTA_HARD_MAX = 255


def get_formant_commands_from_deltas(deltas: Sequence[int]) -> list[str]:
	commands: list[str] = []
	for index, delta in enumerate(deltas):
		if not delta:
			continue
		delta_int = max(FORMANT_DELTA_HARD_MIN, min(FORMANT_DELTA_HARD_MAX, int(delta)))
		sign = "+" if delta_int > 0 else "-"
		hh = min(0xFF, abs(delta_int))
		commands.append(f"@u{index}{hh:02X}{sign} ")
	return commands


def get_formant_adjust_commands(index: int, diff: int) -> list[str]:
	if not diff:
		return []
	sign = "+" if diff > 0 else "-"
	remaining = abs(int(diff))
	commands: list[str] = []
	while remaining > 0:
		chunk = min(0xFF, remaining)
		commands.append(f"@u{index}{chunk:02X}{sign} ")
		remaining -= chunk
	return commands


def get_formant_diff_commands(desired: Sequence[int], applied: Sequence[int]) -> list[str]:
	commands: list[str] = []
	for index, delta in enumerate(desired):
		try:
			current = int(applied[index])
		except Exception:
			current = 0
		diff = int(delta) - current
		if diff:
			commands.extend(get_formant_adjust_commands(index, diff))
	return commands

