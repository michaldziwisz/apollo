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
	EndUtteranceCommand,
	IndexCommand,
	PitchCommand,
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

try:
	import languageHandler  # type: ignore[import-not-found]
except ImportError:
	languageHandler = None  # type: ignore[assignment]


_DEFAULT_PORT = "COM3"
_BAUD_RATE = 9600
_INDEX_POLL_INTERVAL_SECONDS = 0.10
_ROM_INFO_REQUEST_MIN_INTERVAL_SECONDS = 5.0
_ROM_INFO_REQUEST_TIMEOUT_SECONDS = 2.0
_WRITE_CHUNK_SIZE = 128

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
_NAK = b"\x15"

_INTERNAL_DONE_INDEX = -1

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
	generation: int = 0


@dataclass(frozen=True)
class _RomSlotInfo:
	slot: str
	languageCode: Optional[str]
	extension: Optional[str]
	engineVersion: bytes
	languageVersion: bytes
	nvdaLanguage: Optional[str]


_CALLING_CODE4_TO_NVDA_LANGUAGE: dict[str, str] = {
	"0001": "en_US",
	"0031": "nl_NL",
	"0033": "fr_FR",
	"0034": "es_ES",
	"0039": "it_IT",
	"0041": "de_CH",
	"0043": "de_AT",
	"0044": "en_GB",
	"0045": "da_DK",
	"0046": "sv_SE",
	"0047": "nb_NO",
	"0048": "pl_PL",
	"0049": "de_DE",
	"0055": "pt_BR",
	"0351": "pt_PT",
	"0353": "en_IE",
	"0358": "fi_FI",
	"0380": "uk_UA",
	"0420": "cs_CZ",
	"0421": "sk_SK",
}


def _decodeSwappedHexByte(twoAsciiHexDigits: bytes) -> int:
	# Dolphin uses "low nibble first" ASCII hex, e.g. b"40" => 0x04.
	if len(twoAsciiHexDigits) != 2:
		raise ValueError("Expected 2 ASCII hex digits")
	normalized = bytes((twoAsciiHexDigits[1], twoAsciiHexDigits[0])).upper()
	return int(normalized.decode("ascii"), 16)


def _decodeIndexCounter(twoAsciiHexDigits: bytes, pendingCount: int) -> int:
	"""
	Apollo firmware variants disagree on hex digit order for the index counter.
	Try both and prefer a value that fits the number of pending marks.
	"""
	if len(twoAsciiHexDigits) != 2:
		raise ValueError("Expected 2 ASCII hex digits")
	try:
		normal = int(twoAsciiHexDigits.decode("ascii"), 16)
	except Exception:
		normal = 0
	try:
		swapped = _decodeSwappedHexByte(twoAsciiHexDigits)
	except Exception:
		swapped = normal

	candidatesInRange = [v for v in (normal, swapped) if 0 <= v <= pendingCount]
	if candidatesInRange:
		# Prefer the larger value to avoid popping indexes too early.
		return max(candidatesInRange)
	return min(normal, swapped)


def _normalizeNvdaLang(lang: str) -> str:
	lang = (lang or "").strip()
	lang = lang.replace("-", "_")
	if not lang:
		return ""
	parts = lang.split("_")
	if len(parts) == 1:
		return parts[0].lower()
	return f"{parts[0].lower()}_{parts[1].upper()}"


def _apolloLanguageCodeToNvdaLanguage(languageCode: str) -> Optional[str]:
	if not languageCode or len(languageCode) < 5 or not languageCode.isdigit():
		return None
	# Manual: first digit may disambiguate languages within same calling code (e.g. Welsh: 10044).
	variantDigit = languageCode[0]
	callingCode4 = languageCode[-4:]
	if callingCode4 == "0044" and variantDigit == "1":
		return "cy"
	return _CALLING_CODE4_TO_NVDA_LANGUAGE.get(callingCode4)


def _getLanguageDisplayName(nvdaLanguage: Optional[str], fallback: str) -> str:
	if nvdaLanguage and languageHandler:
		try:
			return languageHandler.getLanguageDescription(nvdaLanguage)
		except Exception:
			pass
	return nvdaLanguage or fallback


def _sanitizeText(text: str) -> str:
	if not text:
		return ""
	# Apollo uses @-prefixed commands; don't allow those to leak from NVDA text.
	text = text.replace("@", " ")
	# Normalize all whitespace/control chars to ASCII space to avoid word-join bugs
	# (e.g. tabs / non-breaking spaces not treated as separators by the synth).
	return "".join(
		" " if (ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F) else ch for ch in text
	)


def _encodeText(text: str) -> bytes:
	textWithNumbers = numbers_pl.dajNapisZLiczbamiWPostaciSlownej(text)
	cp1250 = textWithNumbers.encode("cp1250", "replace")
	return cp1250.translate(_POLISH_TO_APOLLO_TRANSLATION)


def _hexDigit(value: int) -> str:
	return f"{value:X}"


class SynthDriver(BaseSynthDriver):
	name = "apollo2"
	description = "Dolphin Apollo 2 (modern)"

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
		CharacterModeCommand,
		EndUtteranceCommand,
		PitchCommand,
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
		self._serialIoLock = threading.Lock()

		self._needsSettingsSync = True
		self._needsRomSwitch = False

		self._writeQueue: queue.Queue[Optional[_WriteItem]] = queue.Queue()
		self._stopEvent = threading.Event()
		self._cancelGeneration = 0

		self._indexLock = threading.Lock()
		self._pendingIndexes: deque[int] = deque()
		self._isSpeaking = False

		self._pollSuspendLock = threading.Lock()
		self._pollSuspendUntil = 0.0

		self._romInfoLock = threading.Lock()
		self._romInfoBySlot: dict[str, _RomSlotInfo] = {}
		self._romInfoLastRequest = 0.0

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

		self._writeThread = threading.Thread(
			target=self._writeLoop,
			name="apolloSynthWrite",
			daemon=True,
		)
		self._writeThread.start()

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

	def _queueWrite(self, data: bytes, indexes: tuple[int, ...] = ()) -> None:
		if not self._stopEvent.is_set():
			self._writeQueue.put(
				_WriteItem(data=data, indexes=indexes, generation=self._cancelGeneration),
			)

	def _getSerial(self) -> Optional[serial.Serial]:  # type: ignore[misc]
		with self._serialLock:
			return self._serial

	def _disconnect(self) -> None:
		with self._serialLock:
			ser = self._serial
			self._serial = None
		self._needsSettingsSync = True
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

		# Reset and enable indexing (used for continuous reading / say all).
		self._queueWrite(_MUTE)
		self._queueWrite(b"@I+ ")
		return True

	def _suspendPolling(self, seconds: float) -> None:
		if seconds <= 0:
			return
		until = time.monotonic() + seconds
		with self._pollSuspendLock:
			self._pollSuspendUntil = max(self._pollSuspendUntil, until)

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
			if item.generation != self._cancelGeneration:
				continue

			shouldTrackIndexes = False
			for offset in range(0, len(item.data), _WRITE_CHUNK_SIZE):
				chunk = item.data[offset : offset + _WRITE_CHUNK_SIZE]
				with self._serialIoLock:
					if item.generation != self._cancelGeneration:
						break
					try:
						ser.write(chunk)
					except Exception:
						log.debugWarning("Apollo serial write failed", exc_info=True)
						break
				self._suspendPollingAfterWrite(len(chunk))
			else:
				# Completed without breaking.
				shouldTrackIndexes = True

			if shouldTrackIndexes and item.indexes:
				with self._indexLock:
					if item.generation == self._cancelGeneration:
						self._pendingIndexes.extend(item.indexes)
						self._isSpeaking = True

	def _pollLoop(self) -> None:
		while not self._stopEvent.is_set():
			with self._pollSuspendLock:
				suspendUntil = self._pollSuspendUntil
			now = time.monotonic()
			if now < suspendUntil:
				time.sleep(min(_INDEX_POLL_INTERVAL_SECONDS, suspendUntil - now))
				continue

			shouldPoll = False
			with self._indexLock:
				shouldPoll = self._isSpeaking or bool(self._pendingIndexes)

			if shouldPoll and self._getSerial() is not None:
				self._queueWrite(b"@I?")
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

			if not first or first == _NAK:
				continue

			if first == b"I":
				try:
					rest = ser.read(3)
					if len(rest) != 3:
						continue
					with self._indexLock:
						pendingCount = len(self._pendingIndexes)
					unitsRemaining = _decodeIndexCounter(rest[:2], pendingCount)
				except Exception:
					continue
				self._onUnitsRemaining(unitsRemaining)
				continue

			if first == b"L":
				self._handleLanguageListResponse(ser)
				continue

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
			if index >= 0:
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
		with self._romInfoLock:
			self._romInfoBySlot.clear()
			self._romInfoLastRequest = 0.0

	def _sendSettingCommand(self, command: str) -> None:
		if self._getSerial() is None:
			self._needsSettingsSync = True
			return
		# Ensure the next text doesn't accidentally join the command stream.
		self._queueWrite((command + " ").encode("ascii", "ignore"))

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
		if self._getSerial() is None:
			self._ensureConnected()
		self._queueRomInfoRequestIfNeeded()
		with self._romInfoLock:
			infoBySlot = dict(self._romInfoBySlot)

		slots: list[str]
		if infoBySlot:
			slots = sorted(infoBySlot.keys(), key=lambda s: int(s) if s.isdigit() else 999)
		else:
			slots = ["1", "2", "3", "4"]

		current = self.rom
		if current and current not in slots:
			slots.append(current)

		roms: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		for slot in slots:
			info = infoBySlot.get(slot)
			if info and info.languageCode:
				displayLang = _getLanguageDisplayName(info.nvdaLanguage, info.languageCode)
				roms[slot] = StringParameterInfo(
					slot,
					_("{slot}: {language}").format(slot=slot, language=displayLang),
				)
			else:
				roms[slot] = StringParameterInfo(slot, _("ROM {slot}").format(slot=slot))
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
		self._needsRomSwitch = True
		self._needsSettingsSync = True
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

	def _settingsPrefix(self, *, rom: Optional[str] = None) -> bytes:
		return (
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

	def _queueRomInfoRequestIfNeeded(self, *, force: bool = False) -> None:
		if self._getSerial() is None:
			return
		with self._romInfoLock:
			hasInfo = bool(self._romInfoBySlot)
			lastRequest = self._romInfoLastRequest
		if hasInfo and not force:
			return
		now = time.monotonic()
		if not force and now - lastRequest < _ROM_INFO_REQUEST_MIN_INTERVAL_SECONDS:
			return
		with self._romInfoLock:
			self._romInfoLastRequest = now
		self._suspendPolling(_ROM_INFO_REQUEST_TIMEOUT_SECONDS)
		self._queueWrite(b"@L")

	def _handleLanguageListResponse(self, ser) -> None:
		deadline = time.monotonic() + _ROM_INFO_REQUEST_TIMEOUT_SECONDS

		def readByte() -> bytes:
			while time.monotonic() < deadline and not self._stopEvent.is_set():
				b = ser.read(1)
				if b:
					return b
			return b""

		def readSwappedHexByte() -> int:
			digits = bytearray()
			while len(digits) < 2 and time.monotonic() < deadline and not self._stopEvent.is_set():
				b = readByte()
				if not b:
					continue
				if b in b"0123456789abcdefABCDEF":
					digits.extend(b)
					continue
			if len(digits) != 2:
				raise TimeoutError
			return _decodeSwappedHexByte(bytes(digits))

		def readNonSeparator() -> bytes:
			while time.monotonic() < deadline and not self._stopEvent.is_set():
				b = readByte()
				if not b:
					continue
				if b in b", \t\r\n":
					continue
				return b
			return b""

		try:
			recordCount = readSwappedHexByte()
			recordSize = readSwappedHexByte()
			if recordCount <= 0 or recordSize <= 0:
				return

			total = recordCount * recordSize
			firstData = readNonSeparator()
			if not firstData:
				return

			data = bytearray(firstData)
			while len(data) < total and time.monotonic() < deadline and not self._stopEvent.is_set():
				chunk = ser.read(total - len(data))
				if not chunk:
					continue
				data.extend(chunk)
			if len(data) < total:
				return
		except Exception:
			log.debugWarning("Failed to parse Apollo language list (@L) response", exc_info=True)
			return

		parsed: dict[str, _RomSlotInfo] = {}
		for index in range(min(recordCount, 4)):
			slot = str(index + 1)
			start = index * recordSize
			end = start + recordSize
			rec = bytes(data[start:end])

			langCodeBytes = rec[:5]
			languageCode = None
			if len(langCodeBytes) == 5 and all(48 <= b <= 57 for b in langCodeBytes):
				languageCode = langCodeBytes.decode("ascii")

			extension = None
			if recordSize >= 6:
				ext = rec[5:6]
				if ext and 32 <= ext[0] <= 126:
					extension = ext.decode("ascii")

			engineVersion = rec[6:10] if recordSize >= 10 else b""
			languageVersion = rec[10:14] if recordSize >= 14 else b""
			nvdaLanguage = _apolloLanguageCodeToNvdaLanguage(languageCode) if languageCode else None

			parsed[slot] = _RomSlotInfo(
				slot=slot,
				languageCode=languageCode,
				extension=extension,
				engineVersion=engineVersion,
				languageVersion=languageVersion,
				nvdaLanguage=nvdaLanguage,
			)

		with self._romInfoLock:
			self._romInfoBySlot = parsed

	def _getRomForNvdaLanguage(self, requestedLang: str) -> Optional[str]:
		requested = _normalizeNvdaLang(requestedLang)
		if not requested:
			return None
		with self._romInfoLock:
			infoBySlot = dict(self._romInfoBySlot)
		if not infoBySlot:
			return None

		requestedBase = requested.split("_")[0]
		for slot, info in infoBySlot.items():
			candidate = _normalizeNvdaLang(info.nvdaLanguage or "")
			if not candidate:
				continue
			if requested == candidate or requestedBase == candidate.split("_")[0]:
				return slot
		return None

	def speak(self, speechSequence):
		if not self._ensureConnected():
			synthDoneSpeaking.notify(synth=self)
			return

		indexes: list[int] = []
		outputParts: list[bytes] = []
		textBufferParts: list[str] = []
		charModeActive = False

		def flushText() -> None:
			if not textBufferParts:
				return
			text = "".join(textBufferParts)
			textBufferParts.clear()
			if text:
				outputParts.append(_encodeText(text))

		def setSpellMode(enabled: bool) -> None:
			outputParts.append(f" @S{1 if enabled else 0} ".encode("ascii", "ignore"))

		if self._needsSettingsSync:
			if self._needsRomSwitch:
				self._queueWrite(f"@={self._rom}, ".encode("ascii", "ignore"))
				self._needsRomSwitch = False
			outputParts.append(self._settingsPrefix())
			self._needsSettingsSync = False

		for item in speechSequence:
			if isinstance(item, str):
				textBufferParts.append(_sanitizeText(item))
				if charModeActive:
					# NVDA doesn't always send CharacterModeCommand(False); apply it only to the
					# immediately following text chunk, then restore user setting.
					flushText()
					setSpellMode(self._spellMode)
					charModeActive = False
			elif isinstance(item, IndexCommand):
				flushText()
				outputParts.append(b" @I+ ")
				indexes.append(item.index)
			elif isinstance(item, CharacterModeCommand):
				flushText()
				if item.state:
					setSpellMode(True)
					charModeActive = True
				else:
					setSpellMode(self._spellMode)
					charModeActive = False
			elif isinstance(item, PitchCommand):
				flushText()
				basePitch = int(getattr(self, "pitch", 50) or 0)
				targetPitch = basePitch
				if getattr(item, "offset", None) is not None:
					targetPitch = basePitch + int(item.offset)
				elif getattr(item, "multiplier", None) is not None:
					targetPitch = int(round(basePitch * float(item.multiplier)))
				targetPitch = max(0, min(100, targetPitch))
				apolloPitch = self._percentToParam(targetPitch, _MIN_PITCH, _MAX_PITCH)
				outputParts.append(f"@F{_hexDigit(apolloPitch)} ".encode("ascii", "ignore"))
			elif isinstance(item, EndUtteranceCommand):
				flushText()
				if charModeActive:
					setSpellMode(self._spellMode)
					charModeActive = False
			elif isinstance(item, BreakCommand):
				flushText()
				if item.time and item.time > 0:
					repeats = max(1, round(item.time / 100))
					outputParts.append(b" @Tx " * repeats)

		if charModeActive:
			flushText()
			setSpellMode(self._spellMode)
			charModeActive = False
		flushText()
		# Always append a final index mark so we can reliably detect end of speech.
		outputParts.append(b" @I+ ")
		indexes.append(_INTERNAL_DONE_INDEX)
		data = b"".join(outputParts) + _CR
		self._queueWrite(data, indexes=tuple(indexes))

	def cancel(self):
		wasSpeaking = False
		with self._indexLock:
			wasSpeaking = self._isSpeaking or bool(self._pendingIndexes)

		# Abort any in-flight write item and discard queued speech immediately.
		self._cancelGeneration += 1
		self._clearIndexes()
		try:
			while True:
				self._writeQueue.get_nowait()
		except queue.Empty:
			pass

		ser = self._getSerial()
		if ser is not None:
			try:
				cancelWrite = getattr(ser, "cancel_write", None)
				if cancelWrite:
					cancelWrite()
			except Exception:
				pass
			with self._serialIoLock:
				try:
					# Drop any pending bytes in the OS TX buffer so the mute command is sent immediately.
					ser.reset_output_buffer()
				except Exception:
					log.debugWarning("Failed to reset Apollo serial output buffer", exc_info=True)
				try:
					ser.write(_MUTE)
				except Exception:
					log.debugWarning("Apollo serial write failed", exc_info=True)
				try:
					ser.write(b"@I+ ")
				except Exception:
					log.debugWarning("Apollo serial write failed", exc_info=True)

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
