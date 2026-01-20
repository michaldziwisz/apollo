# -*- coding: UTF-8 -*-
from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Optional

import addonHandler
from autoSettingsUtils.driverSetting import BooleanDriverSetting, DriverSetting, NumericDriverSetting
from autoSettingsUtils.utils import StringParameterInfo
from logHandler import log
from speech.commands import (
	BreakCommand,
	CharacterModeCommand,
	IndexCommand,
	PhonemeCommand,
	PitchCommand,
	RateCommand,
	VolumeCommand,
)
from synthDriverHandler import SynthDriver as BaseSynthDriver, VoiceInfo, synthDoneSpeaking, synthIndexReached

from . import numbers_pl

addonHandler.initTranslation()

try:
	import serial  # type: ignore[import-not-found]
	from serial import rs485  # type: ignore[import-not-found]
except ImportError:
	from . import cserial as serial  # type: ignore[no-redef]
	from .cserial import rs485  # type: ignore[no-redef]


_DEFAULT_PORT = "COM3"
_BAUD_RATE = 9600
_INDEX_POLL_INTERVAL_SECONDS = 0.10

_MIN_RATE = 1
_MAX_RATE = 9
_MIN_PITCH = 0
_MAX_PITCH = 15
_MIN_VOLUME = 0
_MAX_VOLUME = 15
_MIN_INFLECTION = 0
_MAX_INFLECTION = 7
_MIN_VOICING = 1
_MAX_VOICING = 8
_MIN_SENTENCE_PAUSE = 0
_MAX_SENTENCE_PAUSE = 15
_MIN_WORD_PAUSE = 0
_MAX_WORD_PAUSE = 9
_MIN_MARK_SPACE_RATIO = 0
_MAX_MARK_SPACE_RATIO = 0x3F

_MUTE = b"\x18"
_CR = b"\r"

_POLISH_TO_APOLLO_TRANSLATION = bytes.maketrans(
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


@dataclass(frozen=True)
class _WriteItem:
	data: bytes
	indexes: tuple[int, ...] = ()


def _encodeText(text: str) -> bytes:
	textWithNumbers = numbers_pl.dajNapisZLiczbamiWPostaciSlownej(text)
	cp1250 = textWithNumbers.encode("cp1250", "replace")
	return cp1250.translate(_POLISH_TO_APOLLO_TRANSLATION)


def _hexDigit(value: int) -> str:
	return f"{value:X}"


class SynthDriver(BaseSynthDriver):
	name = "apollo"
	description = "Dolphin Apollo 2"

	supportedSettings = (
		BaseSynthDriver.VoiceSetting(),
		BaseSynthDriver.RateSetting(minStep=5),
		BaseSynthDriver.PitchSetting(minStep=5),
		BaseSynthDriver.VolumeSetting(minStep=5),
		BaseSynthDriver.InflectionSetting(minStep=5),
		BooleanDriverSetting(
			"punctuation",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Punctuation"),
			defaultVal=False,
		),
		BooleanDriverSetting(
			"spellMode",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Spell mode"),
			defaultVal=False,
		),
		BooleanDriverSetting(
			"hypermode",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Hypermode"),
			defaultVal=False,
		),
		BooleanDriverSetting(
			"phoneticMode",
			# Translators: Label for a setting in the voice settings dialog.
			_("P&honetic mode"),
			defaultVal=False,
		),
		NumericDriverSetting(
			"markSpaceRatio",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Mark-space ratio"),
			minStep=1,
		),
		DriverSetting(
			"speakerTable",
			# Translators: Label for a setting in the voice settings dialog.
			_("Speaker &table"),
			defaultVal="0",
		),
		DriverSetting(
			"voiceFilter",
			# Translators: Label for a setting in the voice settings dialog.
			_("Voice source/&filter"),
			defaultVal="0",
		),
		DriverSetting(
			"rom",
			# Translators: Label for a setting in the voice settings dialog.
			_("&ROM slot"),
			defaultVal="1",
		),
		NumericDriverSetting(
			"sentencePause",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Sentence pause"),
			minStep=5,
		),
		NumericDriverSetting(
			"wordPause",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Word pause"),
			minStep=5,
		),
		NumericDriverSetting(
			"voicing",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Voicing"),
			minStep=5,
		),
		DriverSetting(
			"port",
			# Translators: Label for a setting in the voice settings dialog.
			_("Serial &port"),
			defaultVal=_DEFAULT_PORT,
		),
	)
	supportedCommands = {
		IndexCommand,
		BreakCommand,
		PitchCommand,
		RateCommand,
		VolumeCommand,
		CharacterModeCommand,
		PhonemeCommand,
	}
	supportedNotifications = {synthIndexReached, synthDoneSpeaking}

	@classmethod
	def check(cls):
		return True

	def __init__(self):
		super().__init__()

		self._port: str = _DEFAULT_PORT
		self._serial: Optional[serial.Serial] = None  # type: ignore[misc]
		self._serialLock = threading.Lock()

		self._writeQueue: queue.Queue[Optional[_WriteItem]] = queue.Queue()
		self._stopEvent = threading.Event()
		self._writeThread = threading.Thread(
			target=self._writeLoop,
			name="apolloSynthWrite",
			daemon=True,
		)
		self._writeThread.start()

		self._indexLock = threading.Lock()
		self._pendingIndexes: deque[int] = deque()
		self._isSpeaking = False

		self._readThread = threading.Thread(
			target=self._readLoop,
			name="apolloSynthRead",
			daemon=True,
		)
		self._readThread.start()

		self._indexPollThread = threading.Thread(
			target=self._pollLoop,
			name="apolloSynthIndexPoll",
			daemon=True,
		)
		self._indexPollThread.start()

		self._pollSuspendLock = threading.Lock()
		self._pollSuspendUntil = 0.0

		self._rate = 3
		self._pitch = 8
		self._volume = 0xA
		self._inflection = 3
		self._voicing = 8
		self._sentencePause = 0xB
		self._wordPause = 0
		self._voice = "1"
		self._punctuation = False
		self._spellMode = False
		self._hypermode = False
		self._phoneticMode = False
		self._markSpaceRatio = 0x16
		self._speakerTable = "0"
		self._voiceFilter = "0"
		self._rom = "1"

	def _queueWrite(self, data: bytes, indexes: tuple[int, ...] = ()) -> None:
		if not self._stopEvent.is_set():
			self._writeQueue.put(_WriteItem(data=data, indexes=indexes))

	def _getSerial(self) -> Optional[serial.Serial]:  # type: ignore[misc]
		with self._serialLock:
			return self._serial

	def _disconnect(self) -> None:
		with self._serialLock:
			ser = self._serial
			self._serial = None
		if ser is not None:
			try:
				ser.close()
			except Exception:
				log.debugWarning("Failed to close Apollo serial port", exc_info=True)
		self._clearIndexes()

	def _ensureConnected(self) -> bool:
		if self._getSerial() is not None:
			return True

		try:
			ser = serial.serial_for_url(  # type: ignore[attr-defined]
				self._port,
				baudrate=_BAUD_RATE,
				timeout=0.1,
				write_timeout=0.5,
			)
			ser.dsrdtr = False
			try:
				ser.rs485_mode = rs485.RS485Settings()
			except Exception:
				pass
			try:
				ser.reset_input_buffer()
				ser.reset_output_buffer()
			except Exception:
				pass
		except Exception:
			log.error(f"Unable to open Apollo serial port: {self._port}", exc_info=True)
			return False

		with self._serialLock:
			self._serial = ser

		self._queueWrite(_MUTE + b"@1+")
		return True

	def _suspendPollingAfterWrite(self, byteCount: int) -> None:
		# Serial line time ≈ bytes * (start + 8 data + stop) / baud.
		# Add a small safety margin to avoid polling before the synth has received the whole chunk.
		seconds = (byteCount * 10) / _BAUD_RATE + 0.05
		until = time.monotonic() + seconds
		with self._pollSuspendLock:
			self._pollSuspendUntil = max(self._pollSuspendUntil, until)

	def _writeLoop(self) -> None:
		while True:
			item = self._writeQueue.get()
			if item is None:
				return
			ser = self._getSerial()
			if ser is None:
				continue
			try:
				ser.write(item.data)
			except Exception:
				log.debugWarning("Apollo serial write failed", exc_info=True)
				continue

			if item.indexes:
				with self._indexLock:
					self._pendingIndexes.extend(item.indexes)
					self._isSpeaking = True
				self._suspendPollingAfterWrite(len(item.data))

	def _pollLoop(self) -> None:
		while not self._stopEvent.is_set():
			with self._pollSuspendLock:
				suspendUntil = self._pollSuspendUntil
			now = time.monotonic()
			if now < suspendUntil:
				time.sleep(min(_INDEX_POLL_INTERVAL_SECONDS, suspendUntil - now))
				continue
			if self._getSerial() is not None:
				self._queueWrite(b"@1?")
			time.sleep(_INDEX_POLL_INTERVAL_SECONDS)

	def _readLoop(self) -> None:
		while not self._stopEvent.is_set():
			ser = self._getSerial()
			if ser is None:
				time.sleep(0.1)
				continue
			try:
				first = ser.read(1)
			except Exception:
				log.debugWarning("Apollo serial read failed", exc_info=True)
				self._disconnect()
				time.sleep(0.5)
				continue

			if not first or first != b"I":
				continue

			try:
				rest = ser.read(3)
				if len(rest) != 3:
					continue
				unitsRemaining = int(rest[:2].decode("ascii"), 16)
			except Exception:
				continue

			self._onUnitsRemaining(unitsRemaining)

	def _clearIndexes(self) -> None:
		with self._indexLock:
			self._pendingIndexes.clear()
			self._isSpeaking = False

	def _onUnitsRemaining(self, unitsRemaining: int) -> None:
		reached: list[int] = []
		shouldNotifyDone = False

		with self._indexLock:
			while unitsRemaining < len(self._pendingIndexes):
				reached.append(self._pendingIndexes.popleft())
			if self._isSpeaking and not self._pendingIndexes:
				self._isSpeaking = False
				shouldNotifyDone = True

		for index in reached:
			synthIndexReached.notify(synth=self, index=index)
		if shouldNotifyDone:
			synthDoneSpeaking.notify(synth=self)

	def _getAvailableVoices(self):
		voices = OrderedDict()
		# Manual: voices 1-3 are male-based, 4-6 are non-male-based.
		voices["1"] = VoiceInfo("1", _("Voice 1 (male)"))
		voices["2"] = VoiceInfo("2", _("Voice 2 (male)"))
		voices["3"] = VoiceInfo("3", _("Voice 3 (male)"))
		voices["4"] = VoiceInfo("4", _("Voice 4 (non-male)"))
		voices["5"] = VoiceInfo("5", _("Voice 5 (non-male)"))
		voices["6"] = VoiceInfo("6", _("Voice 6 (non-male)"))
		return voices

	def _get_availablePorts(self):
		ports: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		try:
			try:
				from serial.tools import list_ports  # type: ignore[import-not-found]
			except ImportError:
				from .cserial.tools import list_ports  # type: ignore[no-redef]

			for portInfo in list_ports.comports():
				device = portInfo.device
				description = getattr(portInfo, "description", "") or ""
				displayName = f"{device} - {description}" if description else device
				ports[device] = StringParameterInfo(device, displayName)
		except Exception:
			ports[_DEFAULT_PORT] = StringParameterInfo(_DEFAULT_PORT, _DEFAULT_PORT)

		current = self.port
		if current and current not in ports:
			ports[current] = StringParameterInfo(current, current)
		return ports

	def _get_port(self) -> str:
		return self._port

	def _set_port(self, value: str) -> None:
		value = (value or "").strip()
		if not value:
			value = _DEFAULT_PORT
		if value == self._port:
			return
		self._port = value
		self._disconnect()

	def _sendSettingCommand(self, command: str) -> None:
		if self._getSerial() is None:
			return
		self._queueWrite(command.encode("ascii", "ignore"))

	def _get_voice(self) -> str:
		return self._voice

	def _set_voice(self, value: str) -> None:
		self._voice = value
		self._sendSettingCommand(f"@V{value}")

	def _get_rate(self) -> int:
		return self._paramToPercent(self._rate, _MIN_RATE, _MAX_RATE)

	def _set_rate(self, value: int) -> None:
		self._rate = self._percentToParam(value, _MIN_RATE, _MAX_RATE)
		self._sendSettingCommand(f"@W{self._rate}")

	def _get_pitch(self) -> int:
		return self._paramToPercent(self._pitch, _MIN_PITCH, _MAX_PITCH)

	def _set_pitch(self, value: int) -> None:
		self._pitch = self._percentToParam(value, _MIN_PITCH, _MAX_PITCH)
		self._sendSettingCommand(f"@F{_hexDigit(self._pitch)}")

	def _get_volume(self) -> int:
		return self._paramToPercent(self._volume, _MIN_VOLUME, _MAX_VOLUME)

	def _set_volume(self, value: int) -> None:
		self._volume = self._percentToParam(value, _MIN_VOLUME, _MAX_VOLUME)
		self._sendSettingCommand(f"@A{_hexDigit(self._volume)}")

	def _get_inflection(self) -> int:
		return self._paramToPercent(self._inflection, _MIN_INFLECTION, _MAX_INFLECTION)

	def _set_inflection(self, value: int) -> None:
		self._inflection = self._percentToParam(value, _MIN_INFLECTION, _MAX_INFLECTION)
		self._sendSettingCommand(f"@R{self._inflection}")

	def _get_punctuation(self) -> bool:
		return self._punctuation

	def _set_punctuation(self, value: bool) -> None:
		self._punctuation = bool(value)
		self._sendSettingCommand(f"@P{1 if self._punctuation else 0}")

	def _get_spellMode(self) -> bool:
		return self._spellMode

	def _set_spellMode(self, value: bool) -> None:
		self._spellMode = bool(value)
		self._sendSettingCommand(f"@S{1 if self._spellMode else 0}")

	def _get_hypermode(self) -> bool:
		return self._hypermode

	def _set_hypermode(self, value: bool) -> None:
		self._hypermode = bool(value)
		self._sendSettingCommand(f"@H{1 if self._hypermode else 0}")

	def _get_phoneticMode(self) -> bool:
		return self._phoneticMode

	def _set_phoneticMode(self, value: bool) -> None:
		self._phoneticMode = bool(value)
		self._sendSettingCommand(f"@X{1 if self._phoneticMode else 0}")

	def _get_markSpaceRatio(self) -> int:
		return self._paramToPercent(self._markSpaceRatio, _MIN_MARK_SPACE_RATIO, _MAX_MARK_SPACE_RATIO)

	def _set_markSpaceRatio(self, value: int) -> None:
		self._markSpaceRatio = self._percentToParam(value, _MIN_MARK_SPACE_RATIO, _MAX_MARK_SPACE_RATIO)
		self._sendSettingCommand(f"@M{self._markSpaceRatio:02X}")

	def _get_availableSpeakertables(self):
		tables: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		tables["0"] = StringParameterInfo("0", _("Male"))
		tables["1"] = StringParameterInfo("1", _("Non-male"))
		current = self.speakerTable
		if current and current not in tables:
			tables[current] = StringParameterInfo(current, current)
		return tables

	def _get_speakerTable(self) -> str:
		return self._speakerTable

	def _set_speakerTable(self, value: str) -> None:
		value = (value or "").strip()
		if value not in ("0", "1"):
			value = "0"
		self._speakerTable = value
		self._sendSettingCommand(f"@K{value}")

	def _get_availableVoicefilters(self):
		filters: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		filters["0"] = StringParameterInfo("0", _("Male (default)"))
		filters["1"] = StringParameterInfo("1", _("Female (default)"))
		filters["2"] = StringParameterInfo("2", _("Male (spike)"))
		filters["3"] = StringParameterInfo("3", _("Female (spike)"))
		filters["4"] = StringParameterInfo("4", _("Male (cut-down default)"))
		filters["5"] = StringParameterInfo("5", _("Female (cut-down default)"))
		filters["6"] = StringParameterInfo("6", _("Male (reduced high-frequency filter)"))
		filters["7"] = StringParameterInfo("7", _("Female (reduced high-frequency filter)"))
		current = self.voiceFilter
		if current and current not in filters:
			filters[current] = StringParameterInfo(current, current)
		return filters

	def _get_voiceFilter(self) -> str:
		return self._voiceFilter

	def _set_voiceFilter(self, value: str) -> None:
		value = (value or "").strip()
		if value not in ("0", "1", "2", "3", "4", "5", "6", "7"):
			value = "0"
		self._voiceFilter = value
		self._sendSettingCommand(f"@${value}")

	def _get_availableRoms(self):
		roms: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		roms["1"] = StringParameterInfo("1", _("ROM 1"))
		roms["2"] = StringParameterInfo("2", _("ROM 2"))
		roms["3"] = StringParameterInfo("3", _("ROM 3"))
		roms["4"] = StringParameterInfo("4", _("ROM 4"))
		current = self.rom
		if current and current not in roms:
			roms[current] = StringParameterInfo(current, current)
		return roms

	def _get_rom(self) -> str:
		return self._rom

	def _set_rom(self, value: str) -> None:
		value = (value or "").strip()
		if value not in ("1", "2", "3", "4"):
			value = "1"
		if value == self._rom:
			return
		self._rom = value
		# Selecting a ROM might reset the synth; reconnect on next utterance.
		self._disconnect()

	def _get_sentencePause(self) -> int:
		return self._paramToPercent(self._sentencePause, _MIN_SENTENCE_PAUSE, _MAX_SENTENCE_PAUSE)

	def _set_sentencePause(self, value: int) -> None:
		self._sentencePause = self._percentToParam(value, _MIN_SENTENCE_PAUSE, _MAX_SENTENCE_PAUSE)
		self._sendSettingCommand(f"@D{_hexDigit(self._sentencePause)}")

	def _get_wordPause(self) -> int:
		return self._paramToPercent(self._wordPause, _MIN_WORD_PAUSE, _MAX_WORD_PAUSE)

	def _set_wordPause(self, value: int) -> None:
		self._wordPause = self._percentToParam(value, _MIN_WORD_PAUSE, _MAX_WORD_PAUSE)
		self._sendSettingCommand(f"@Q{self._wordPause}")

	def _get_voicing(self) -> int:
		return self._paramToPercent(self._voicing, _MIN_VOICING, _MAX_VOICING)

	def _set_voicing(self, value: int) -> None:
		self._voicing = self._percentToParam(value, _MIN_VOICING, _MAX_VOICING)
		self._sendSettingCommand(f"@B{self._voicing}")

	def _settingsPrefix(self) -> bytes:
		return (
			f"@={self._rom}, "
			f"@K{self._speakerTable} "
			f"@${self._voiceFilter} "
			f"@P{1 if self._punctuation else 0} "
			f"@S{1 if self._spellMode else 0} "
			f"@H{1 if self._hypermode else 0} "
			f"@X{1 if self._phoneticMode else 0} "
			f"@M{self._markSpaceRatio:02X} "
			f"@V{self._voice} "
			f"@W{self._rate} "
			f"@F{_hexDigit(self._pitch)} "
			f"@A{_hexDigit(self._volume)} "
			f"@R{self._inflection} "
			f"@B{self._voicing} "
			f"@D{_hexDigit(self._sentencePause)} "
			f"@Q{self._wordPause} "
		).encode("ascii", "ignore")

	def speak(self, speechSequence):
		self.cancel()
		if not self._ensureConnected():
			synthDoneSpeaking.notify(synth=self)
			return

		indexes: list[int] = []
		outputParts: list[bytes] = [self._settingsPrefix()]
		textBufferParts: list[str] = []

		def flushText() -> None:
			if not textBufferParts:
				return
			text = "".join(textBufferParts)
			textBufferParts.clear()
			if text.strip():
				outputParts.append(_encodeText(text))

		for item in speechSequence:
			if isinstance(item, str):
				cleaned = item.replace("@", " ").replace("\r", " ").replace("\n", " ")
				textBufferParts.append(cleaned)
			elif isinstance(item, IndexCommand):
				flushText()
				outputParts.append(b" @l+ ")
				indexes.append(item.index)
			elif isinstance(item, BreakCommand):
				flushText()
				repeats = max(1, round(item.time / 100)) if item.time else 1
				outputParts.append(b" @Tx " * repeats)
			elif isinstance(item, PitchCommand):
				flushText()
				percent = max(0, min(100, item.newValue))
				raw = self._percentToParam(percent, _MIN_PITCH, _MAX_PITCH)
				outputParts.append(f"@F{_hexDigit(raw)} ".encode("ascii", "ignore"))
			elif isinstance(item, RateCommand):
				flushText()
				percent = max(0, min(100, item.newValue))
				raw = self._percentToParam(percent, _MIN_RATE, _MAX_RATE)
				outputParts.append(f"@W{raw} ".encode("ascii", "ignore"))
			elif isinstance(item, VolumeCommand):
				flushText()
				percent = max(0, min(100, item.newValue))
				raw = self._percentToParam(percent, _MIN_VOLUME, _MAX_VOLUME)
				outputParts.append(f"@A{_hexDigit(raw)} ".encode("ascii", "ignore"))
			elif isinstance(item, CharacterModeCommand):
				flushText()
				outputParts.append(f"@S{1 if item.state else 0} ".encode("ascii", "ignore"))
			elif isinstance(item, PhonemeCommand):
				flushText()
				if item.text:
					cleaned = item.text.replace("@", " ").replace("\r", " ").replace("\n", " ")
					textBufferParts.append(cleaned)

		flushText()
		data = b"".join(outputParts) + _CR
		self._queueWrite(data, indexes=tuple(indexes))

		if not indexes:
			synthDoneSpeaking.notify(synth=self)

	def cancel(self):
		wasSpeaking = False
		with self._indexLock:
			wasSpeaking = self._isSpeaking or bool(self._pendingIndexes)

		self._clearIndexes()
		try:
			while True:
				self._writeQueue.get_nowait()
		except queue.Empty:
			pass

		if self._ensureConnected():
			self._queueWrite(_MUTE + b"@1+")

		if wasSpeaking:
			synthDoneSpeaking.notify(synth=self)

	def pause(self, switch):
		if switch:
			self.cancel()

	def terminate(self):
		self.cancel()
		self._stopEvent.set()
		self._writeQueue.put(None)
		self._disconnect()
		super().terminate()
