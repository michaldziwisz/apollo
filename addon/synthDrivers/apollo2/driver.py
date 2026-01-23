# -*- coding: UTF-8 -*-
from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Optional, Sequence

import addonHandler
from autoSettingsUtils.driverSetting import BooleanDriverSetting, DriverSetting
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

from .formants import (
	FORMANT_DELTA_HARD_MAX as _FORMANT_DELTA_HARD_MAX,
	FORMANT_DELTA_HARD_MIN as _FORMANT_DELTA_HARD_MIN,
	get_formant_commands_from_deltas,
	get_formant_diff_commands,
)
from .indexing import decode_index_counter, decode_swapped_hex_byte
from .protocol import (
	CR as _CR,
	INDEX_ENABLE_COMMAND,
	INDEX_MARK_COMMAND,
	INDEX_QUERY_COMMAND,
	MUTE as _MUTE,
	NAK as _NAK,
)
from .text import encode_text, sanitize_text

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
_AUTO_PORT = "auto"
_DEFAULT_BAUD_RATE = 9600
# Apollo is most stable (and power-up defaults) at 9600 baud. We intentionally only support 9600
# to avoid probing/operating at other rates which can cause false negatives and unstable behavior
# on some USB-serial adapters / firmware variants.
_SUPPORTED_BAUD_RATES = (9600,)
# How long we wait for a valid indexing probe when NVDA switches to this synth.
# If the configured port is wrong (or the device is missing), failing fast prevents NVDA
# from going silent and lets it keep using the previously selected synthesizer.
_INITIAL_CONNECT_MAX_SECONDS = 2.0
_BAUD_RATE_TO_APOLLO_SELECTOR: dict[int, str] = {9600: "3"}
_INDEX_POLL_INTERVAL_SECONDS = 0.10
_ROM_INFO_REQUEST_MIN_INTERVAL_SECONDS = 5.0
_ROM_INFO_REQUEST_TIMEOUT_SECONDS = 2.0
_Y_BAUD_SWITCH_MAX_SECONDS = 1.5
_Y_BAUD_SWITCH_PROBE_TIMEOUT_SECONDS = 0.25
# Smaller chunks improve responsiveness when cancelling speech (more frequent generation checks)
# while staying well within typical USB-serial driver buffering.
_WRITE_CHUNK_SIZE = 64
_OFFLINE_WRITE_MAX_AGE_SECONDS = 10.0
_OFFLINE_WRITE_RETRY_INTERVAL_SECONDS = 0.25
_SETTINGS_SYNC_DEBOUNCE_SECONDS = 0.05
_CONNECT_BACKOFF_ON_WRITE_TIMEOUT_SECONDS = 2.0

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
_FORMANT_DELTA_UI_DEFAULT_MAX_ABS = 50
# Apply formant tweaks as soon as possible. A previous debounce here made adjustments feel
# inconsistent (the spoken value could be rendered before the new setting took effect) and could
# temporarily stall speech while the write thread waited for the debounce window.
_FORMANT_SYNC_DEBOUNCE_SECONDS = 0.0

_INTERNAL_DONE_INDEX = -1
_NVDA_STARTUP_ANNOUNCE_WINDOW_SECONDS = 30.0
_nvdaStartupAnnounced = False


@dataclass(frozen=True)
class _WriteItem:
	data: bytes
	indexes: tuple[int, ...] = ()
	generation: int = 0
	createdAt: float = 0.0
	includesSettings: bool = False
	cancelable: bool = True
	isSettingsSync: bool = False
	isFormantSync: bool = False
	isMute: bool = False


def _is_hex_digit_byte(b: bytes) -> bool:
	return bool(b) and b[0] in b"0123456789abcdefABCDEF"


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


def _hexDigit(value: int) -> str:
	return f"{value:X}"


class SynthDriver(BaseSynthDriver):
	name = "apollo2"
	description = "Dolphin Apollo 2 (modern)"

	supportedSettings = (
		BaseSynthDriver.VoiceSetting(),
		BaseSynthDriver.RateSetting(minStep=13),
		BaseSynthDriver.PitchSetting(minStep=7),
		BaseSynthDriver.VolumeSetting(minStep=7),
		BaseSynthDriver.InflectionSetting(minStep=15),
		DriverSetting(
			"formantDeltaUiRange",
			# Translators: Label for an advanced setting controlling the size of formant tuning lists.
			_("Advanced: Formant tuning &range"),
			availableInSettingsRing=False,
			defaultVal=str(_FORMANT_DELTA_UI_DEFAULT_MAX_ABS),
		),
		# Advanced: formant adjustments. These use relative +/- commands (@u…hh+/-), so they are applied
		# deterministically by doing a soft reset (@J) during settings sync when any of them are non-zero.
		DriverSetting(
			"formantFnDelta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: Fn (nasal formant frequency) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantF1Delta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: F1 (first formant frequency) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantF2Delta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: F2 (second formant frequency) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantF3Delta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: F3 (third formant frequency) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantAlfDelta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: ALF (low frequency amplitude) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantA1Delta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: A1 (first formant amplitude) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantA2Delta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: A2 (second formant amplitude) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantA3Delta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: A3 (third formant amplitude) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantA4Delta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: A4 (fourth formant amplitude) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		DriverSetting(
			"formantIvDelta",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: IV (degree of voicing) delta"),
			availableInSettingsRing=False,
			defaultVal="0",
		),
		BooleanDriverSetting(
			"resetFormantTuning",
			# Translators: Label for an advanced setting in the voice settings dialog.
			_("Advanced: Reset formant tuning to defaults"),
			availableInSettingsRing=False,
			defaultVal=False,
		),
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
		BooleanDriverSetting(
			"expandNumbers",
			# Translators: Label for a setting in the voice settings dialog.
			_("Expand &numbers to words"),
			defaultVal=False,
		),
		DriverSetting(
			"markSpaceRatio",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Mark-space ratio"),
			defaultVal="22",
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
		DriverSetting(
			"sentencePause",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Sentence pause"),
			defaultVal="11",
		),
		DriverSetting(
			"wordPause",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Word pause"),
			defaultVal="0",
		),
		DriverSetting(
			"voicing",
			# Translators: Label for a setting in the voice settings dialog.
			_("&Voicing"),
			defaultVal="8",
		),
		DriverSetting(
			"port",
			# Translators: Label for a setting in the voice settings dialog.
			_("Serial &port"),
			defaultVal=_AUTO_PORT,
		),
		DriverSetting(
			"baudRate",
			# Translators: Label for a setting in the voice settings dialog.
			_("Serial &baud rate"),
			defaultVal=str(_DEFAULT_BAUD_RATE),
		),
		BooleanDriverSetting(
			"announceNvdaStartup",
			# Translators: Label for a setting in the voice settings dialog.
			_("Announce NVDA &startup message"),
			defaultVal=True,
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

		self._port: str = _AUTO_PORT
		self._baudRate: int = _DEFAULT_BAUD_RATE
		self._serial: Optional[serial.Serial] = None  # type: ignore[misc]
		self._serialLock = threading.Lock()
		# Needs to be re-entrant because `_disconnect()` may be called from within a write while this
		# lock is already held.
		self._serialIoLock = threading.RLock()
		self._writeStateLock = threading.Lock()
		self._isWritingSpeech = False
		self._connectLock = threading.Lock()
		self._connectThread: Optional[threading.Thread] = None
		self._lastConnectErrorLogTime = 0.0
		self._lastIndexResponseTime = 0.0
		self._pendingApplyBaudRate = False
		self._pendingApplyBaudRateTarget: Optional[int] = None
		self._connectBackoffUntil = 0.0
		self._serialWriteTimeoutCount = 0
		# Indexing commands (used for Say All / speech cancellation). Earlier driver versions found
		# that the "@1?" / "@1+" variant could result in stray "1" announcements, so we stick to
		# the "@I?" / "@I+" form.
		self._indexQueryCommand = INDEX_QUERY_COMMAND
		self._indexEnableCommand = INDEX_ENABLE_COMMAND
		self._indexMarkCommand = INDEX_MARK_COMMAND

		self._settingsRevision = 0
		self._settingsLastChangedAt = 0.0
		self._settingsSyncQueued = False
		self._needsSettingsSync = True
		self._needsRomSwitch = False
		self._formantSyncQueued = False
		self._formantRevision = 0
		self._formantSyncDueAt = 0.0

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
		self._expandNumbers = False
		self._markSpaceRatio = 0x16
		self._speakerTable = "0"
		self._voiceFilter = "0"
		self._formantDeltaUiRange = str(_FORMANT_DELTA_UI_DEFAULT_MAX_ABS)
		self._formantDeltas = [0] * 10
		self._formantDeltasApplied = [0] * 10
		self._needsSoftReset = True
		# Revision marker for soft reset requests to avoid races between the UI thread (changing settings)
		# and the write thread clearing the flag after a sync.
		self._softResetRequestedRevision = 0
		self._rom = "1"
		self._announceNvdaStartup = True
		self._driverInitTime = time.monotonic()
		self._didInitialConnectCheck = False
		self._lastDetectedPort: Optional[str] = None
		self._isLoadingSettings = False

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

	def loadSettings(self):
		# While NVDA is applying profile values, suppress background connection attempts and serial writes.
		# This prevents a failed synth switch (wrong port/device missing) from leaving background threads
		# attempting to connect and holding the COM port.
		self._isLoadingSettings = True
		try:
			super().loadSettings()
			# Load a cached auto-detect hint (if present). This lets "Auto (detect)" avoid scanning.
			try:
				import config  # type: ignore[import-not-found]

				speechSection = config.conf.get("speech") if hasattr(config, "conf") else None
				driverSection = speechSection.get(self.name) if speechSection else None
				cachedPort = driverSection.get("lastDetectedPort") if driverSection else None
				if cachedPort:
					self._lastDetectedPort = str(cachedPort)
			except Exception:
				pass
		finally:
			self._isLoadingSettings = False

		# Safety: perform a bounded connection attempt during synth switch/startup.
		# If we can't talk to Apollo quickly (wrong port/device missing), fail so NVDA can keep
		# speaking via the previously active synthesizer.
		if not self._didInitialConnectCheck:
			self._didInitialConnectCheck = True
			if not self._ensureConnected(maxDuration=_INITIAL_CONNECT_MAX_SECONDS):
				raise RuntimeError(
						f"Apollo not detected (port={self._port}, baud={self._baudRate}). "
						"Check the serial port selection or use 'Auto (detect)'."
					)

		# NVDA can call `loadSettings()` after some initial speech has already been queued during startup.
		# Ensure Apollo is re-synced to the loaded profile values even if the Voice Settings dialog is
		# never opened.
		self._requireSettingsSync()
		# Connect in the background so opening the Voice Settings dialog never blocks the UI.
		self._startBackgroundConnect()
		self._queueSettingsSync()

	def _touchSettingsRevision(self) -> None:
		self._settingsRevision += 1
		self._settingsLastChangedAt = time.monotonic()

	def _requireSettingsSync(self) -> None:
		self._needsSettingsSync = True

	def _startBackgroundConnect(self) -> None:
		if self._stopEvent.is_set():
			return
		if self._isLoadingSettings:
			return
		now = time.monotonic()
		if now < self._connectBackoffUntil:
			return
		if self._getSerial() is not None:
			return
		thread = self._connectThread
		if thread is not None and thread.is_alive():
			return
		self._connectThread = threading.Thread(
			target=self._ensureConnected,
			name="apolloSynthConnect",
			daemon=True,
		)
		self._connectThread.start()

	def _queueWrite(
		self,
		data: bytes,
		indexes: tuple[int, ...] = (),
		*,
		includesSettings: bool = False,
		cancelable: bool = True,
		isSettingsSync: bool = False,
		isFormantSync: bool = False,
		isMute: bool = False,
	) -> None:
		if not self._stopEvent.is_set():
			self._writeQueue.put(
				_WriteItem(
					data=data,
					indexes=indexes,
					generation=self._cancelGeneration,
					createdAt=time.monotonic(),
					includesSettings=includesSettings,
					cancelable=cancelable,
					isSettingsSync=isSettingsSync,
					isFormantSync=isFormantSync,
					isMute=isMute,
				),
			)

	def _getSerial(self) -> Optional[serial.Serial]:  # type: ignore[misc]
		with self._serialLock:
			return self._serial

	def _disconnect(self) -> None:
		# Ensure we never close the port while another thread is in the middle of a write/flush.
		with self._serialIoLock:
			with self._serialLock:
				ser = self._serial
				self._serial = None
		self._needsSoftReset = True
		self._formantDeltasApplied = [0] * 10
		self._requireSettingsSync()
		if ser is not None:
			# Abort any in-flight I/O so close doesn't block and pending writes don't delay later speech.
			try:
				cancelWrite = getattr(ser, "cancel_write", None)
				if callable(cancelWrite):
					cancelWrite()
			except Exception:
				pass
			try:
				cancelRead = getattr(ser, "cancel_read", None)
				if callable(cancelRead):
					cancelRead()
			except Exception:
				pass
			try:
				ser.close()
			except Exception:
				log.debugWarning("Failed to close Apollo serial port", exc_info=True)
		self._clearIndexes()

	def _probeIndexResponse(self, ser: serial.Serial, *, timeout: float = 0.35) -> bool:  # type: ignore[misc]
		before = self._lastIndexResponseTime
		with self._serialIoLock:
			try:
				ser.write(self._indexQueryCommand)
				try:
					ser.flush()
				except Exception:
					pass
			except Exception:
				return False
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline and not self._stopEvent.is_set():
			if self._lastIndexResponseTime > before:
				return True
			time.sleep(0.01)
		return False

	def _initPort(self, ser: serial.Serial) -> None:  # type: ignore[misc]
		# Reset and enable indexing (used for continuous reading / say all).
		with self._serialIoLock:
			try:
				ser.write(_MUTE)
				try:
					ser.flush()
				except Exception:
					pass
			except Exception:
				log.debugWarning("Apollo serial write failed", exc_info=True)
			try:
				ser.write(self._indexEnableCommand)
				try:
					ser.flush()
				except Exception:
					pass
			except Exception:
				log.debugWarning("Apollo serial write failed", exc_info=True)

	def _queueSettingsSync(self) -> None:
		if self._stopEvent.is_set() or not self._needsSettingsSync:
			return
		if self._isLoadingSettings:
			return
		if self._settingsSyncQueued:
			return
		self._settingsSyncQueued = True
		# Queue a placeholder sync item. The write thread will generate the settings bytes just-in-time
		# and will ensure special commands (like @J) are sent in separate writes.
		self._queueWrite(
			b"",
			includesSettings=True,
			cancelable=False,
			isSettingsSync=True,
		)

	def _queueFormantSync(self) -> None:
		if self._stopEvent.is_set():
			return
		if self._isLoadingSettings:
			return
		if self._formantSyncQueued:
			return
		self._formantSyncQueued = True
		self._queueWrite(b"", cancelable=False, isFormantSync=True)

	def _ensureConnected(self, *, maxDuration: Optional[float] = None) -> bool:
		with self._connectLock:
			overallDeadline: Optional[float] = None
			if maxDuration is not None and maxDuration > 0:
				overallDeadline = time.monotonic() + maxDuration
			now = time.monotonic()
			if now < self._connectBackoffUntil:
				return False
			if self._getSerial() is not None:
				return True

			def isPortBusyError(exc: Exception) -> bool:
				msg = str(exc)
				return (
					"PermissionError" in msg
					or "Access is denied" in msg
					or "Odmowa dostępu" in msg
					or "errno 13" in msg
				)

			connectReasons: list[str] = []
			sawBusyPortError = False
			desiredBaudRate = self._baudRate if self._baudRate in _SUPPORTED_BAUD_RATES else _DEFAULT_BAUD_RATE
			baudTryOrder: list[int] = []

			def addBaud(rate: int) -> None:
				if rate in _SUPPORTED_BAUD_RATES and rate not in baudTryOrder:
					baudTryOrder.append(rate)

			# Apollo is most stable at its power-up default 9600 baud. Avoid probing multiple baud rates
			# during synth switch/startup to reduce false negatives and unexpected behavior.
			addBaud(desiredBaudRate)
			addBaud(_DEFAULT_BAUD_RATE)
			if overallDeadline is None and desiredBaudRate != _DEFAULT_BAUD_RATE:
				for rate in _SUPPORTED_BAUD_RATES:
					addBaud(rate)

			def getCandidatePorts() -> tuple[str, ...]:
				requested = (self._port or "").strip() or _DEFAULT_PORT
				if requested != _AUTO_PORT:
					return (requested,)
				try:
					try:
						from serial.tools import list_ports  # type: ignore[import-not-found]
					except ImportError:
						from .cserial.tools import list_ports  # type: ignore[no-redef]
					candidates = [p.device for p in list_ports.comports() if getattr(p, "device", None)]
				except Exception:
					candidates = []
				# Prefer the last successfully detected port (if any) to avoid scanning.
				cached = (self._lastDetectedPort or "").strip()
				if cached and cached in candidates:
					candidates.remove(cached)
					candidates.insert(0, cached)
				if _DEFAULT_PORT not in candidates:
					candidates.append(_DEFAULT_PORT)
				seen: set[str] = set()
				result: list[str] = []
				for p in candidates:
					p = (p or "").strip()
					if not p or p in seen or p == _AUTO_PORT:
						continue
					seen.add(p)
					result.append(p)
				return tuple(result) if result else (_DEFAULT_PORT,)

			def openSerial(port: str, baudRate: int) -> Optional[serial.Serial]:  # type: ignore[misc]
				# Keep write timeouts short but realistic: a single chunk must be able to leave the OS
				# buffer at the current baud rate, otherwise writes will always fail (especially on 300/1200).
				# This also bounds how long `_serialIoLock` can block other threads.
				minWriteTimeout = (_WRITE_CHUNK_SIZE * 10) / max(baudRate, 1) + 0.2
				try:
					ser = serial.serial_for_url(  # type: ignore[attr-defined]
						port,
						baudrate=baudRate,
						timeout=0.1,
						write_timeout=minWriteTimeout,
						rtscts=False,
						dsrdtr=False,
						xonxoff=False,
					)
					# Apollo uses RTS as a direction/handshake line for 2-way comms.
					# Use pyserial RS485 mode (RTS high for TX, low for RX) to avoid blocking replies
					# from the device (indexing, @c? queries, etc.).
					try:
						ser.dsrdtr = False
					except Exception:
						pass
					try:
						ser.rs485_mode = rs485.RS485Settings()
					except Exception:
						pass
					# Keep DTR asserted (common expectation for older serial peripherals).
					try:
						ser.dtr = True
					except Exception:
						pass
					try:
						ser.reset_input_buffer()
						ser.reset_output_buffer()
					except Exception:
						pass
					return ser
				except Exception as e:
					connectReasons.append(f"{port}@{baudRate} open failed: {type(e).__name__}: {e}")
					nonlocal sawBusyPortError
					if isPortBusyError(e):
						sawBusyPortError = True
					return None

			def closeSerial(ser: Optional[serial.Serial]) -> None:  # type: ignore[misc]
				if ser is None:
					return
				try:
					ser.close()
				except Exception:
					log.debugWarning("Failed to close Apollo serial port during connect", exc_info=True)

			def writeAndFlush(ser: serial.Serial, data: bytes) -> bool:  # type: ignore[misc]
				with self._serialIoLock:
					try:
						ser.write(data)
						try:
							ser.flush()
						except Exception:
							pass
						return True
					except Exception:
						return False

			def probeIndexResponseDirect(
				ser: serial.Serial,
				*,
				command: bytes,
				timeout: float = 0.35,
			) -> bool:  # type: ignore[misc]
				if overallDeadline is not None:
					remaining = overallDeadline - time.monotonic()
					if remaining <= 0:
						return False
					timeout = min(timeout, remaining)
				# Probe Apollo indexing without relying on the background read thread.
				try:
					ser.reset_input_buffer()
				except Exception:
					pass
				if not writeAndFlush(ser, command):
					return False
				deadline = time.monotonic() + timeout
				while time.monotonic() < deadline and not self._stopEvent.is_set():
					try:
						first = ser.read(1)
					except Exception:
						return False
					if not first or first == _NAK:
						continue
					if first != b"I":
						continue
					try:
						rest = ser.read(3)
					except Exception:
						return False
					if len(rest) != 3:
						return False
					# Validate the response shape to avoid false positives at the wrong baud rate.
					hexDigits = b"0123456789abcdefABCDEF"
					if rest[0:1] not in hexDigits or rest[1:2] not in hexDigits:
						return False
					if rest[2:3] not in (b"T", b"M", b"t", b"m"):
						return False
					return True
				return False

			def probeSettingResponseDirect(
				ser: serial.Serial,
				*,
				command: bytes,
				expectedPrefix: bytes,
				timeout: float = 0.35,
			) -> bool:  # type: ignore[misc]
				"""Probe a 3-byte "@c?" response (e.g. @V? -> Vhh)."""
				if overallDeadline is not None:
					remaining = overallDeadline - time.monotonic()
					if remaining <= 0:
						return False
					timeout = min(timeout, remaining)
				try:
					ser.reset_input_buffer()
				except Exception:
					pass
				# Some firmware variants only process "@c?" queries after a delimiter, so include a
				# trailing space.
				if not writeAndFlush(ser, command + b" "):
					return False
				deadline = time.monotonic() + timeout
				while time.monotonic() < deadline and not self._stopEvent.is_set():
					try:
						first = ser.read(1)
					except Exception:
						return False
					if not first or first == _NAK:
						continue
					if first != expectedPrefix:
						continue
					try:
						rest = ser.read(2)
					except Exception:
						return False
					if len(rest) != 2:
						return False
					if not _is_hex_digit_byte(rest[0:1]) or not _is_hex_digit_byte(rest[1:2]):
						return False
					return True
				return False

			def ensureIndexingAndProbe(
				ser: serial.Serial,
				*,
				port: str,
				baudRate: int,
			) -> bool:  # type: ignore[misc]
				probeTimeout = 0.35 if overallDeadline is None else 0.25

				def tryIndexingProbe(*, query: bytes, enable: bytes) -> bool:
					writeAndFlush(ser, _MUTE)
					if probeIndexResponseDirect(ser, command=query + b" ", timeout=probeTimeout):
						self._indexQueryCommand = query
						self._indexEnableCommand = enable
						self._indexMarkCommand = enable
						return True
					writeAndFlush(ser, enable)
					if probeIndexResponseDirect(ser, command=query + b" ", timeout=probeTimeout):
						self._indexQueryCommand = query
						self._indexEnableCommand = enable
						self._indexMarkCommand = enable
						return True
					return False

				# Use "@I?" / "@I+" for indexing by default (prevents stray "1" announcements on some firmware),
				# but fall back to "@1?" / "@1+" if that is the variant supported by the device.
				if tryIndexingProbe(query=self._indexQueryCommand, enable=self._indexEnableCommand):
					return True
				if tryIndexingProbe(query=b"@1?", enable=b"@1+ "):
					log.info("Apollo: using @1?/@1+ indexing command variant.")
					return True
				return False

			def trySwitchSynthBaudRate(ser: serial.Serial, *, port: str, currentBaud: int) -> Optional[int]:
				if overallDeadline is not None and time.monotonic() > overallDeadline:
					return None
				# Only try @Y switching when explicitly requested by the user via the one-shot action.
				if not self._pendingApplyBaudRate:
					return currentBaud
				targetBaud = self._pendingApplyBaudRateTarget or desiredBaudRate
				self._pendingApplyBaudRate = False
				self._pendingApplyBaudRateTarget = None
				if targetBaud == currentBaud:
					log.info(f"Apollo: @Y switch not needed; already at {targetBaud}.")
					return currentBaud
				selector = _BAUD_RATE_TO_APOLLO_SELECTOR.get(targetBaud)
				if selector is None:
					log.warning(f"Apollo: baud {targetBaud} has no @Y selector; staying at {currentBaud}.")
					return currentBaud

				log.info(
					f"Apollo: switching synthesizer baud rate {currentBaud} -> {targetBaud} using @Y (one-shot).",
				)

				# NOTE: @Y is special: after the command bytes, Apollo expects 5 sync bytes at the new
				# baud rate to confirm synchronization. If the sync fails, the synth may revert to the
				# power-up default settings.
				#
				# Keep this handshake short: some adapters/firmware combinations can block for multiple
				# seconds when probing at the wrong baud rate. A long @Y attempt delays speech startup
				# and feels like NVDA has frozen.
				handshakeDeadline = time.monotonic() + _Y_BAUD_SWITCH_MAX_SECONDS
				baudCommands = (
					# Compact form first (some firmware expects no separators).
					f"@Yf{selector}N8".encode("ascii", "ignore"),
					f"@YF{selector}N8".encode("ascii", "ignore"),
					# Documented form (with separators).
					f"@Y f {selector} N 8".encode("ascii", "ignore"),
					f"@Y F {selector} N 8".encode("ascii", "ignore"),
				)
				syncPayload = b"\x55" * 5

				switchSucceeded = False
				prevTimeout = getattr(ser, "timeout", None)
				prevWriteTimeout = getattr(ser, "write_timeout", None)
				try:
					# Reduce timeouts while probing so a failing @Y attempt can't stall NVDA for long.
					ser.timeout = min(prevTimeout or _Y_BAUD_SWITCH_PROBE_TIMEOUT_SECONDS, _Y_BAUD_SWITCH_PROBE_TIMEOUT_SECONDS)
					ser.write_timeout = min(
						prevWriteTimeout or _Y_BAUD_SWITCH_PROBE_TIMEOUT_SECONDS,
						_Y_BAUD_SWITCH_PROBE_TIMEOUT_SECONDS,
					)
				except Exception:
					pass

				try:
					for baudCommand in baudCommands:
						if time.monotonic() > handshakeDeadline:
							break

						# Ensure the synth is quiet before the @Y handshake.
						writeAndFlush(ser, _MUTE)
						time.sleep(0.01)
						if not writeAndFlush(ser, baudCommand):
							continue

						try:
							# Switch the host to the new baud rate immediately after the command bytes are flushed.
							ser.baudrate = targetBaud
						except Exception as e:
							connectReasons.append(
								f"{port}@{currentBaud} host baud switch to {targetBaud} failed: {type(e).__name__}: {e}",
							)
							try:
								ser.baudrate = currentBaud
							except Exception:
								pass
							return currentBaud

						# Send sync bytes (0x55 == ASCII 'U') as recommended for UART synchronization.
						if not writeAndFlush(ser, syncPayload):
							log.warning("Apollo: @Y sync write failed; attempting to recover.")

						# Give the synth a moment to settle before probing.
						time.sleep(0.05)

						if probeIndexResponseDirect(
							ser,
							command=self._indexQueryCommand,
							timeout=_Y_BAUD_SWITCH_PROBE_TIMEOUT_SECONDS,
						):
							switchSucceeded = True
							break

						# Failed: revert host baud so we can try the next variant / recover.
						try:
							ser.baudrate = currentBaud
						except Exception:
							pass
						time.sleep(0.02)
				finally:
					try:
						if prevTimeout is not None:
							ser.timeout = prevTimeout
					except Exception:
						pass
					try:
						if prevWriteTimeout is not None:
							ser.write_timeout = prevWriteTimeout
					except Exception:
						pass

				if switchSucceeded:
					log.info(f"Apollo: synth baud rate switched successfully to {targetBaud}.")
					return targetBaud

				# Recovery: try to re-probe at the previous baud, then at the power-up default.
				for fallbackBaud in (currentBaud, _DEFAULT_BAUD_RATE):
					try:
						ser.baudrate = fallbackBaud
					except Exception:
						continue
					if ensureIndexingAndProbe(ser):
						elapsed = _Y_BAUD_SWITCH_MAX_SECONDS - max(0.0, handshakeDeadline - time.monotonic())
						log.warning(
							f"Apollo: @Y switch to {targetBaud} failed after ~{elapsed:.1f}s; recovered at {fallbackBaud}.",
						)
						return fallbackBaud

				log.error(
					f"Apollo: @Y switch to {targetBaud} failed and recovery failed; reconnect required.",
				)
				return None

			def finalizeConnection(port: str, baudRate: int, ser: serial.Serial) -> bool:  # type: ignore[misc]
				# Ensure the synth is in a known state before letting other threads write.
				self._initPort(ser)
				self._serialWriteTimeoutCount = 0
				self._connectBackoffUntil = 0.0
				with self._serialLock:
					self._serial = ser
					log.info(
						"Apollo indexing commands: query=%r enable=%r mark=%r",
						self._indexQueryCommand,
						self._indexEnableCommand,
						self._indexMarkCommand,
					)
					if self._port == _AUTO_PORT:
						log.info(f"Apollo detected on {port} at {baudRate} baud.")
						# Persist the detected port so "Auto (detect)" can prefer it next time.
						if port and port != self._lastDetectedPort:
							self._lastDetectedPort = port
							try:
								import config  # type: ignore[import-not-found]

								speechSection = config.conf.get("speech") if hasattr(config, "conf") else None
								if speechSection is not None:
									driverSection = speechSection.get(self.name)
									if driverSection is None:
										speechSection[self.name] = {}
										driverSection = speechSection.get(self.name)
									if driverSection is not None:
										driverSection["lastDetectedPort"] = port
										try:
											config.conf.save()
										except Exception:
											pass
							except Exception:
								pass
					else:
						log.info(f"Apollo connected on {port} at {baudRate} baud.")
					return True

			while True:
				sawBusyPortError = False
				for port in getCandidatePorts():
					if overallDeadline is not None and time.monotonic() > overallDeadline:
						break
					for baudRate in baudTryOrder:
						if overallDeadline is not None and time.monotonic() > overallDeadline:
							break
						ser = openSerial(port, baudRate)
						if ser is None:
							continue
						if ensureIndexingAndProbe(ser, port=port, baudRate=baudRate):
							finalBaud = baudRate
							switched = trySwitchSynthBaudRate(ser, port=port, currentBaud=baudRate)
							if switched is None:
								closeSerial(ser)
								continue
							finalBaud = switched
							return finalizeConnection(port, finalBaud, ser)
						connectReasons.append(f"{port}@{baudRate} probe failed")
						closeSerial(ser)

				# If the port was temporarily busy, wait a moment and retry within the allowed budget.
				if overallDeadline is None:
					break
				remaining = overallDeadline - time.monotonic()
				if remaining <= 0:
					break
				if not sawBusyPortError:
					break
				time.sleep(min(0.1, remaining))

			# Avoid log spam when some other NVDA component temporarily grabs the port.
			now = time.monotonic()
			if now - self._lastConnectErrorLogTime > 5.0:
				self._lastConnectErrorLogTime = now
				reason = "; ".join(connectReasons[-3:])
				if reason:
					log.error(f"Apollo connection failed (port={self._port}): {reason}")
				else:
					log.error(f"Apollo connection failed (port={self._port})")
			return False

	def _suspendPolling(self, seconds: float) -> None:
		if seconds <= 0:
			return
		until = time.monotonic() + seconds
		with self._pollSuspendLock:
			self._pollSuspendUntil = max(self._pollSuspendUntil, until)

	def _suspendPollingAfterWrite(self, byteCount: int) -> None:
		# Serial line time ≈ bytes * (start + 8 data + stop) / baud.
		# Add a small safety margin to avoid polling before the synth has received the whole chunk.
		ser = self._getSerial()
		baud = getattr(ser, "baudrate", None) if ser is not None else None
		seconds = (byteCount * 10) / (baud or _DEFAULT_BAUD_RATE) + 0.05
		until = time.monotonic() + seconds
		with self._pollSuspendLock:
			self._pollSuspendUntil = max(self._pollSuspendUntil, until)

	def _writeLoop(self) -> None:
		def writeBytes(
			ser: serial.Serial,  # type: ignore[misc]
			data: bytes,
			*,
			cancelable: bool,
			generation: int,
			flush: bool = False,
		) -> bool:
			SerialTimeoutException = getattr(serial, "SerialTimeoutException", None)

			def noteWriteTimeout() -> None:
				self._serialWriteTimeoutCount += 1
				backoff = min(
					30.0,
					_CONNECT_BACKOFF_ON_WRITE_TIMEOUT_SECONDS * (2 ** max(0, self._serialWriteTimeoutCount - 1)),
				)
				self._connectBackoffUntil = max(self._connectBackoffUntil, time.monotonic() + backoff)

			if not data:
				return True
			if flush:
				# Serialize the whole write+flush so cancel() can't drop non-cancelable settings bytes
				# from the OS TX buffer (this would desynchronize formant tuning and other settings).
				with self._serialIoLock:
					if cancelable and generation != self._cancelGeneration:
						return False
					try:
						for offset in range(0, len(data), _WRITE_CHUNK_SIZE):
							chunk = data[offset : offset + _WRITE_CHUNK_SIZE]
							ser.write(chunk)
						try:
							ser.flush()
						except Exception:
							pass
					except Exception as e:
						if SerialTimeoutException is not None and isinstance(e, SerialTimeoutException):
							noteWriteTimeout()
						log.debugWarning("Apollo serial write failed", exc_info=True)
						self._disconnect()
						return False
				self._suspendPollingAfterWrite(len(data))
				return True

			for offset in range(0, len(data), _WRITE_CHUNK_SIZE):
				chunk = data[offset : offset + _WRITE_CHUNK_SIZE]
				with self._serialIoLock:
					if cancelable and generation != self._cancelGeneration:
						return False
					try:
						ser.write(chunk)
					except Exception as e:
						if SerialTimeoutException is not None and isinstance(e, SerialTimeoutException):
							noteWriteTimeout()
						log.debugWarning("Apollo serial write failed", exc_info=True)
						self._disconnect()
						return False
				self._suspendPollingAfterWrite(len(chunk))
			return True
		while True:
			item = self._writeQueue.get()
			if item is None:
				return
			if item.cancelable and item.generation != self._cancelGeneration:
				continue

			if item.isMute:
				ser = self._getSerial()
				if ser is None:
					continue
				with self._serialIoLock:
					try:
						ser.reset_output_buffer()
					except Exception:
						pass
					try:
						ser.write(item.data)
						try:
							ser.flush()
						except Exception:
							pass
					except Exception:
						log.debugWarning("Apollo serial mute failed", exc_info=True)
						self._disconnect()
						continue
				self._suspendPollingAfterWrite(len(item.data))
				continue

			if item.isSettingsSync:
				try:
					while self._needsSettingsSync and not self._stopEvent.is_set():
						# Coalesce rapid setting changes (e.g. scrolling through formant tuning values)
						# so we don't flood the serial link with redundant full syncs and soft resets.
						lastChangedAt = self._settingsLastChangedAt
						if lastChangedAt:
							dueAt = lastChangedAt + _SETTINGS_SYNC_DEBOUNCE_SECONDS
							now = time.monotonic()
							if now < dueAt:
								time.sleep(min(0.05, dueAt - now))
								continue

						ser = self._getSerial()
						if ser is None:
							if not self._ensureConnected():
								time.sleep(_OFFLINE_WRITE_RETRY_INTERVAL_SECONDS)
								continue
							ser = self._getSerial()
							if ser is None:
								time.sleep(_OFFLINE_WRITE_RETRY_INTERVAL_SECONDS)
								continue

						startRevision = self._settingsRevision

						# Selecting a ROM may reset the synth; apply ROM selection before syncing settings.
						if self._needsRomSwitch:
							if not writeBytes(
								ser,
								f"@={self._rom}, ".encode("ascii", "ignore") + _CR,
								cancelable=False,
								generation=item.generation,
								flush=True,
							):
								continue
							self._needsRomSwitch = False

						formantDeltasSnapshot = list(self._formantDeltas)
						needsSoftReset = self._needsSoftReset
						if needsSoftReset:
							formantCommands = self._getFormantCommandsFromDeltas(formantDeltasSnapshot)
						else:
							formantCommands = self._getFormantDiffCommands(
								formantDeltasSnapshot,
								self._formantDeltasApplied,
							)
						if needsSoftReset:
							log.debug("Apollo: sending @J (soft reset) before applying formant deltas.")
							# Must be a separate write (some firmware drops bytes following @J).
							if not writeBytes(
								ser,
								b"@J " + _CR,
								cancelable=False,
								generation=item.generation,
								flush=True,
							):
								continue
						if formantCommands:
							log.debug(f"Apollo: applying formant deltas: {''.join(formantCommands).strip()}")

						if not writeBytes(
							ser,
							self._settingsPrefix(formantCommands=formantCommands) + _CR,
							cancelable=False,
							generation=item.generation,
							flush=True,
						):
							continue

						# Clear the soft reset flag only if no newer setting change requested another reset.
						# Otherwise, the UI thread may set `_needsSoftReset = True` while a sync is in-flight,
						# and we must not clobber it here (it would re-enable diff-based @u updates, causing
						# path-dependent tuning again).
						if self._softResetRequestedRevision <= startRevision:
							self._needsSoftReset = False
						self._formantDeltasApplied = list(formantDeltasSnapshot)
						self._needsSettingsSync = self._settingsRevision != startRevision

						# Avoid busy-looping if the user is changing settings continuously.
						if self._needsSettingsSync:
							time.sleep(0.01)
				finally:
					# Allow a new sync item to be queued if needed.
					self._settingsSyncQueued = self._needsSettingsSync
				continue

			if item.isFormantSync:
				try:
					while not self._stopEvent.is_set():
						# Settings sync already includes formant deltas.
						if self._needsSettingsSync:
							break

						ser = self._getSerial()
						if ser is None:
							if not self._ensureConnected():
								time.sleep(_OFFLINE_WRITE_RETRY_INTERVAL_SECONDS)
								continue
							ser = self._getSerial()
							if ser is None:
								time.sleep(_OFFLINE_WRITE_RETRY_INTERVAL_SECONDS)
								continue

						startRevision = self._formantRevision
						settingsRevisionSnapshot = self._settingsRevision
						desiredSnapshot = list(self._formantDeltas)

						needsSoftReset = self._needsSoftReset
						if needsSoftReset:
							log.debug("Apollo: sending @J (soft reset) before applying formant deltas.")
							# Must be a separate write (some firmware drops bytes following @J).
							if not writeBytes(
								ser,
								b"@J " + _CR,
								cancelable=False,
								generation=item.generation,
								flush=True,
							):
								continue

						if needsSoftReset:
							commands = self._getFormantCommandsFromDeltas(desiredSnapshot)
						else:
							commands = self._getFormantDiffCommands(desiredSnapshot, self._formantDeltasApplied)

						if commands:
							log.debug(f"Apollo: applying formant deltas: {''.join(commands).strip()}")
							if not writeBytes(
								ser,
								"".join(commands).encode("ascii", "ignore"),
								cancelable=False,
								generation=item.generation,
								flush=True,
							):
								continue

							if self._softResetRequestedRevision <= settingsRevisionSnapshot:
								self._needsSoftReset = False
						self._formantDeltasApplied = list(desiredSnapshot)
						if self._formantRevision == startRevision:
							break
						# Avoid busy-looping if the user is changing sliders continuously.
						time.sleep(0.01)
				finally:
					# Allow a new formant sync item to be queued if needed.
					self._formantSyncQueued = False
				continue

			ser = self._getSerial()
			if ser is None:
				if item.cancelable and time.monotonic() - item.createdAt > _OFFLINE_WRITE_MAX_AGE_SECONDS:
					if item.indexes and item.indexes[-1] == _INTERNAL_DONE_INDEX:
						synthDoneSpeaking.notify(synth=self)
					continue
				if not self._ensureConnected():
					time.sleep(_OFFLINE_WRITE_RETRY_INTERVAL_SECONDS)
					self._writeQueue.put(item)
					continue
				ser = self._getSerial()
				if ser is None:
					time.sleep(_OFFLINE_WRITE_RETRY_INTERVAL_SECONDS)
					self._writeQueue.put(item)
					continue

			writingSpeech = bool(item.indexes) and item.cancelable and bool(item.data)
			if writingSpeech:
				with self._writeStateLock:
					self._isWritingSpeech = True
			try:
				if not writeBytes(
					ser,
					item.data,
					cancelable=item.cancelable,
					generation=item.generation,
					flush=not item.cancelable,
				):
					continue
			finally:
				if writingSpeech:
					with self._writeStateLock:
						self._isWritingSpeech = False

				if item.indexes:
					with self._indexLock:
						if not item.cancelable or item.generation == self._cancelGeneration:
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
				self._queueWrite(self._indexQueryCommand)
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
					unitsRemaining = decode_index_counter(rest[:2], pendingCount)
				except Exception:
					continue
				self._lastIndexResponseTime = time.monotonic()
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
		ports[_AUTO_PORT] = StringParameterInfo(_AUTO_PORT, _("Auto (detect)"))
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

	def _get_availableBaudrates(self):
		rates: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		for rate in _SUPPORTED_BAUD_RATES:
			key = str(rate)
			display = f"{rate}"
			if rate == _DEFAULT_BAUD_RATE:
				# Translators: Shown after the baud rate that Apollo uses after power-up.
				display = f"{rate} ({_('default')})"
			rates[key] = StringParameterInfo(key, display)
		current = self.baudRate
		if current and current not in rates:
			rates[current] = StringParameterInfo(current, current)
		return rates

	def _get_availableBaudRates(self):
		return self._get_availableBaudrates()

	def _get_port(self) -> str:
		return self._port

	def _set_port(self, value: str) -> None:
		value = (value or "").strip()
		if not value:
			value = _AUTO_PORT
		if value == self._port:
			return
		self._port = value
		self._disconnect()
		with self._romInfoLock:
			self._romInfoBySlot.clear()
			self._romInfoLastRequest = 0.0

	def _get_baudRate(self) -> str:
		return str(int(self._baudRate or _DEFAULT_BAUD_RATE))

	def _set_baudRate(self, value: str | int) -> None:
		try:
			baudRate = int(value)
		except Exception:
			baudRate = _DEFAULT_BAUD_RATE
		if baudRate not in _SUPPORTED_BAUD_RATES:
			baudRate = _DEFAULT_BAUD_RATE
		if baudRate == self._baudRate:
			return
		self._baudRate = baudRate
		self._disconnect()
		self._startBackgroundConnect()
		self._queueSettingsSync()

	def _get_applyBaudRateNow(self) -> bool:
		# Action setting (always off); checking it triggers an attempt.
		return False

	def _set_applyBaudRateNow(self, value: bool) -> None:
		if not value:
			return
		target = self._baudRate if self._baudRate in _SUPPORTED_BAUD_RATES else _DEFAULT_BAUD_RATE
		self._pendingApplyBaudRate = True
		self._pendingApplyBaudRateTarget = target
		log.info(f"Apollo: user requested @Y baud switch to {target}.")
		# Reconnect so the @Y handshake can run before the read thread starts consuming bytes.
		self._disconnect()
		self._startBackgroundConnect()
		self._queueSettingsSync()

	def _sendSettingCommand(self, command: str) -> None:
		if self._getSerial() is None:
			self._requireSettingsSync()
			self._queueSettingsSync()
			return
		# Ensure the next text doesn't accidentally join the command stream.
		self._queueWrite((command + " ").encode("ascii", "ignore"), cancelable=False)

	def _get_announceNvdaStartup(self) -> bool:
		return self._announceNvdaStartup

	def _set_announceNvdaStartup(self, value: bool) -> None:
		self._announceNvdaStartup = bool(value)

	def _get_voice(self) -> str:
		return self._voice

	def _set_voice(self, value: str) -> None:
		if value == self._voice:
			return
		self._voice = value
		self._touchSettingsRevision()
		if any(self._formantDeltas):
			# Switching preset voices may reset underlying formant parameters; force re-apply.
			self._needsSoftReset = True
			self._formantDeltasApplied = [0] * 10
			self._softResetRequestedRevision = self._settingsRevision
		# Selecting a preset voice can implicitly reset other voice parameters on some firmware.
		# Always re-sync the full settings prefix to keep state deterministic (speaker table, filter,
		# formant deltas, etc.).
		self._requireSettingsSync()
		self._queueSettingsSync()

	def _get_rate(self) -> int:
		return self._paramToPercent(self._rate, _MIN_RATE, _MAX_RATE)

	def _set_rate(self, value: int) -> None:
		rate = self._percentToParam(value, _MIN_RATE, _MAX_RATE)
		if rate == self._rate:
			return
		self._rate = rate
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@W{rate}")

	def _get_pitch(self) -> int:
		return self._paramToPercent(self._pitch, _MIN_PITCH, _MAX_PITCH)

	def _set_pitch(self, value: int) -> None:
		pitch = self._percentToParam(value, _MIN_PITCH, _MAX_PITCH)
		if pitch == self._pitch:
			return
		self._pitch = pitch
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@F{_hexDigit(pitch)}")

	def _get_volume(self) -> int:
		return self._paramToPercent(self._volume, _MIN_VOLUME, _MAX_VOLUME)

	def _set_volume(self, value: int) -> None:
		volume = self._percentToParam(value, _MIN_VOLUME, _MAX_VOLUME)
		if volume == self._volume:
			return
		self._volume = volume
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@A{_hexDigit(volume)}")

	def _get_inflection(self) -> int:
		return self._paramToPercent(self._inflection, _MIN_INFLECTION, _MAX_INFLECTION)

	def _set_inflection(self, value: int) -> None:
		inflection = self._percentToParam(value, _MIN_INFLECTION, _MAX_INFLECTION)
		if inflection == self._inflection:
			return
		self._inflection = inflection
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@R{inflection}")

	def _get_punctuation(self) -> bool:
		return self._punctuation

	def _set_punctuation(self, value: bool) -> None:
		punctuation = bool(value)
		if punctuation == self._punctuation:
			return
		self._punctuation = punctuation
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@P{1 if punctuation else 0}")

	def _get_spellMode(self) -> bool:
		return self._spellMode

	def _set_spellMode(self, value: bool) -> None:
		spellMode = bool(value)
		if spellMode == self._spellMode:
			return
		self._spellMode = spellMode
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@S{1 if spellMode else 0}")

	def _get_hypermode(self) -> bool:
		return self._hypermode

	def _set_hypermode(self, value: bool) -> None:
		hypermode = bool(value)
		if hypermode == self._hypermode:
			return
		self._hypermode = hypermode
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@H{1 if hypermode else 0}")

	def _get_phoneticMode(self) -> bool:
		return self._phoneticMode

	def _set_phoneticMode(self, value: bool) -> None:
		phoneticMode = bool(value)
		if phoneticMode == self._phoneticMode:
			return
		self._phoneticMode = phoneticMode
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@X{1 if phoneticMode else 0}")

	def _get_expandNumbers(self) -> bool:
		return bool(self._expandNumbers)

	def _set_expandNumbers(self, value: bool) -> None:
		self._expandNumbers = bool(value)

	def _coerceChoiceValueToParam(
		self,
		value: object,
		*,
		paramMin: int,
		paramMax: int,
		default: int,
	) -> int:
		if value is None:
			return default
		if isinstance(value, bool):
			return default
		if isinstance(value, (int, float)):
			percent = max(0, min(100, int(value)))
			return self._percentToParam(percent, paramMin, paramMax)

		text = str(value).strip()
		if not text:
			return default

		parsed: Optional[int] = None
		try:
			if text.lower().startswith("0x"):
				parsed = int(text, 16)
			else:
				parsed = int(text, 10)
		except Exception:
			parsed = None

		if parsed is None and len(text) == 1:
			try:
				parsed = int(text, 16)
			except Exception:
				parsed = None

		if parsed is None:
			return default
		if paramMin <= parsed <= paramMax:
			return parsed

		# Legacy configs may contain 0..100 "percent" values from older slider-based settings.
		if 0 <= parsed <= 100:
			return self._percentToParam(parsed, paramMin, paramMax)

		return max(paramMin, min(paramMax, parsed))

	def _get_availableMarkspaceratios(self):
		ratios: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		for ratio in range(_MIN_MARK_SPACE_RATIO, _MAX_MARK_SPACE_RATIO + 1):
			key = str(ratio)
			ratios[key] = StringParameterInfo(key, f"{ratio} (0x{ratio:02X})")

		current = self.markSpaceRatio
		if current and current not in ratios:
			ratios[current] = StringParameterInfo(current, current)
		return ratios

	def _get_markSpaceRatio(self) -> str:
		return str(self._markSpaceRatio)

	def _set_markSpaceRatio(self, value: str | int) -> None:
		markSpaceRatio = self._coerceChoiceValueToParam(
			value,
			paramMin=_MIN_MARK_SPACE_RATIO,
			paramMax=_MAX_MARK_SPACE_RATIO,
			default=self._markSpaceRatio,
		)
		if markSpaceRatio == self._markSpaceRatio:
			return
		self._markSpaceRatio = markSpaceRatio
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@M{markSpaceRatio:02X}")

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
		if value == self._speakerTable:
			return
		self._speakerTable = value
		self._touchSettingsRevision()
		if any(self._formantDeltas):
			self._needsSoftReset = True
			self._formantDeltasApplied = [0] * 10
			self._softResetRequestedRevision = self._settingsRevision
		self._requireSettingsSync()
		self._queueSettingsSync()

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

	def _get_availableSpeakerTables(self):
		return self._get_availableSpeakertables()

	def _get_voiceFilter(self) -> str:
		return self._voiceFilter

	def _get_availableVoiceFilters(self):
		return self._get_availableVoicefilters()

	def _set_voiceFilter(self, value: str) -> None:
		value = (value or "").strip()
		if value not in ("0", "1", "2", "3", "4", "5", "6", "7"):
			value = "0"
		if value == self._voiceFilter:
			return
		self._voiceFilter = value
		self._touchSettingsRevision()
		if any(self._formantDeltas):
			self._needsSoftReset = True
			self._formantDeltasApplied = [0] * 10
			self._softResetRequestedRevision = self._settingsRevision
		self._requireSettingsSync()
		self._queueSettingsSync()

	def _get_formantDeltaUiRange(self) -> str:
		return str(self._formantDeltaUiRange)

	def _set_formantDeltaUiRange(self, value: str) -> None:
		value = (value or "").strip()
		if value not in ("50", "255"):
			value = str(_FORMANT_DELTA_UI_DEFAULT_MAX_ABS)
		self._formantDeltaUiRange = value

	def _get_availableFormantdeltauiranges(self):
		ranges: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		ranges["50"] = StringParameterInfo("50", _("±50 (recommended, faster UI)"))
		ranges["255"] = StringParameterInfo("255", _("±255 (full range, slower UI)"))
		current = self.formantDeltaUiRange
		if current and current not in ranges:
			ranges[current] = StringParameterInfo(current, current)
		return ranges

	def _get_availableRoms(self):
		if self._getSerial() is None:
			# Never block the UI thread (Voice Settings dialog) on serial I/O.
			self._startBackgroundConnect()
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
		self._touchSettingsRevision()
		self._requireSettingsSync()
		self._queueSettingsSync()
		# Selecting a ROM might reset the synth; reconnect on next utterance.
		self._disconnect()

	def _get_availableSentencepauses(self):
		pauses: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		for pause in range(_MIN_SENTENCE_PAUSE, _MAX_SENTENCE_PAUSE + 1):
			key = str(pause)
			pauses[key] = StringParameterInfo(key, f"{pause} (0x{pause:X})")

		current = self.sentencePause
		if current and current not in pauses:
			pauses[current] = StringParameterInfo(current, current)
		return pauses

	def _get_sentencePause(self) -> str:
		return str(self._sentencePause)

	def _set_sentencePause(self, value: str | int) -> None:
		sentencePause = self._coerceChoiceValueToParam(
			value,
			paramMin=_MIN_SENTENCE_PAUSE,
			paramMax=_MAX_SENTENCE_PAUSE,
			default=self._sentencePause,
		)
		if sentencePause == self._sentencePause:
			return
		self._sentencePause = sentencePause
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@D{_hexDigit(sentencePause)}")

	def _get_availableWordpauses(self):
		pauses: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		for pause in range(_MIN_WORD_PAUSE, _MAX_WORD_PAUSE + 1):
			key = str(pause)
			pauses[key] = StringParameterInfo(key, key)

		current = self.wordPause
		if current and current not in pauses:
			pauses[current] = StringParameterInfo(current, current)
		return pauses

	def _get_wordPause(self) -> str:
		return str(self._wordPause)

	def _set_wordPause(self, value: str | int) -> None:
		wordPause = self._coerceChoiceValueToParam(
			value,
			paramMin=_MIN_WORD_PAUSE,
			paramMax=_MAX_WORD_PAUSE,
			default=self._wordPause,
		)
		if wordPause == self._wordPause:
			return
		self._wordPause = wordPause
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@Q{wordPause}")

	def _get_availableVoicings(self):
		voicings: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		for voicing in range(_MIN_VOICING, _MAX_VOICING + 1):
			key = str(voicing)
			voicings[key] = StringParameterInfo(key, key)

		current = self.voicing
		if current and current not in voicings:
			voicings[current] = StringParameterInfo(current, current)
		return voicings

	def _get_voicing(self) -> str:
		return str(self._voicing)

	def _set_voicing(self, value: str | int) -> None:
		voicing = self._coerceChoiceValueToParam(
			value,
			paramMin=_MIN_VOICING,
			paramMax=_MAX_VOICING,
			default=self._voicing,
		)
		if voicing == self._voicing:
			return
		self._voicing = voicing
		self._touchSettingsRevision()
		self._sendSettingCommand(f"@B{voicing}")

	def _getFormantCommandsFromDeltas(self, deltas: Sequence[int]) -> list[str]:
		return get_formant_commands_from_deltas(deltas)

	def _getFormantCommands(self) -> list[str]:
		return self._getFormantCommandsFromDeltas(self._formantDeltas)

	def _getFormantDiffCommands(self, desired: Sequence[int], applied: Sequence[int]) -> list[str]:
		return get_formant_diff_commands(desired, applied)

	def _settingsPrefix(self, *, rom: Optional[str] = None, formantCommands: Optional[list[str]] = None) -> bytes:
		if formantCommands is None:
			formantCommands = self._getFormantCommands()

		commands: list[str] = []
		# Some Apollo firmware variants reset the voice filter when selecting a preset voice (`@V`).
		# Apply `@V` first, then override speaker table / filter.
		commands.extend(
			(
				f"@V{self._voice} ",
				f"@K{self._speakerTable} ",
				f"@${self._voiceFilter} ",
				f"@P{1 if self._punctuation else 0} ",
				f"@S{1 if self._spellMode else 0} ",
				f"@H{1 if self._hypermode else 0} ",
				f"@X{1 if self._phoneticMode else 0} ",
				f"@M{self._markSpaceRatio:02X} ",
				f"@W{self._rate} ",
				f"@F{_hexDigit(self._pitch)} ",
				f"@A{_hexDigit(self._volume)} ",
				f"@R{self._inflection} ",
				f"@B{self._voicing} ",
				f"@D{_hexDigit(self._sentencePause)} ",
				f"@Q{self._wordPause} ",
			),
		)
		commands.extend(formantCommands)
		return "".join(commands).encode("ascii", "ignore")

	def _setFormantDelta(self, index: int, value: str | int) -> None:
		try:
			delta = int(value)
		except Exception:
			delta = 0
		delta = max(_FORMANT_DELTA_HARD_MIN, min(_FORMANT_DELTA_HARD_MAX, delta))
		if index < 0 or index >= len(self._formantDeltas):
			return
		if delta == self._formantDeltas[index]:
			return
		self._formantDeltas[index] = delta
		# These formant commands are relative (+/-) adjustments with no query API. Some ROM variants
		# appear to clamp internal parameters; using incremental diffs can therefore become path-dependent
		# (e.g. reaching the same displayed value via different routes yields different sound). To make
		# tuning deterministic, always re-baseline via @J and resend the full settings.
		self._needsSoftReset = True
		self._formantDeltasApplied = [0] * 10
		self._touchSettingsRevision()
		self._softResetRequestedRevision = self._settingsRevision
		self._requireSettingsSync()
		self._queueSettingsSync()
		return

	def _get_resetFormantTuning(self) -> bool:
		# Action setting (always off); checking it triggers a reset.
		return False

	def _set_resetFormantTuning(self, value: bool) -> None:
		if not value:
			return
		for i in range(len(self._formantDeltas)):
			self._formantDeltas[i] = 0

		# Always force a soft reset so the underlying parameters return to a known baseline, even if
		# deltas already read as 0 in NVDA (the hardware could still be modified from a previous session).
		self._needsSoftReset = True
		self._formantDeltasApplied = [0] * 10
		self._touchSettingsRevision()
		self._softResetRequestedRevision = self._settingsRevision
		self._requireSettingsSync()
		self._queueSettingsSync()

	def _getFormantDeltaUiMaxAbs(self) -> int:
		try:
			maxAbs = int(self._formantDeltaUiRange)
		except Exception:
			maxAbs = _FORMANT_DELTA_UI_DEFAULT_MAX_ABS
		if maxAbs not in (50, 255):
			maxAbs = _FORMANT_DELTA_UI_DEFAULT_MAX_ABS
		return maxAbs

	def _get_availableFormantDeltaValues(self, *, maxAbs: int):
		cache = getattr(self, "_availableFormantDeltaValuesCacheByMaxAbs", None)
		if cache is None:
			cache = {}
			setattr(self, "_availableFormantDeltaValuesCacheByMaxAbs", cache)
		cached = cache.get(maxAbs)
		if cached is not None:
			return cached

		values: "OrderedDict[str, StringParameterInfo]" = OrderedDict()
		for delta in range(-maxAbs, maxAbs + 1):
			key = str(delta)
			if delta > 0:
				display = f"+{delta}"
			elif delta == 0:
				# Translators: Displayed in formant tuning lists; indicates the default (no change).
				display = _("0 (default)")
			else:
				display = key
			values[key] = StringParameterInfo(key, display)

		cache[maxAbs] = values
		return values

	def _get_availableFormantDeltaValuesForIndex(self, index: int):
		maxAbs = self._getFormantDeltaUiMaxAbs()
		values = self._get_availableFormantDeltaValues(maxAbs=maxAbs)

		try:
			currentDelta = int(self._formantDeltas[index])
		except Exception:
			currentDelta = 0
		currentKey = str(currentDelta)
		if currentKey in values:
			return values

		if currentDelta > 0:
			display = f"+{currentDelta}"
		elif currentDelta == 0:
			display = _("0 (default)")
		else:
			display = currentKey

		extended = OrderedDict(values)
		extended[currentKey] = StringParameterInfo(currentKey, display)
		return extended

	def _get_availableFormantfndeltas(self):
		return self._get_availableFormantDeltaValuesForIndex(0)

	def _get_availableFormantf1deltas(self):
		return self._get_availableFormantDeltaValuesForIndex(1)

	def _get_availableFormantf2deltas(self):
		return self._get_availableFormantDeltaValuesForIndex(2)

	def _get_availableFormantf3deltas(self):
		return self._get_availableFormantDeltaValuesForIndex(3)

	def _get_availableFormantalfdeltas(self):
		return self._get_availableFormantDeltaValuesForIndex(4)

	def _get_availableFormanta1deltas(self):
		return self._get_availableFormantDeltaValuesForIndex(5)

	def _get_availableFormanta2deltas(self):
		return self._get_availableFormantDeltaValuesForIndex(6)

	def _get_availableFormanta3deltas(self):
		return self._get_availableFormantDeltaValuesForIndex(7)

	def _get_availableFormanta4deltas(self):
		return self._get_availableFormantDeltaValuesForIndex(8)

	def _get_availableFormantivdeltas(self):
		return self._get_availableFormantDeltaValuesForIndex(9)

	def _get_formantFnDelta(self) -> str:
		return str(int(self._formantDeltas[0]))

	def _set_formantFnDelta(self, value: str | int) -> None:
		self._setFormantDelta(0, value)

	def _get_formantF1Delta(self) -> str:
		return str(int(self._formantDeltas[1]))

	def _set_formantF1Delta(self, value: str | int) -> None:
		self._setFormantDelta(1, value)

	def _get_formantF2Delta(self) -> str:
		return str(int(self._formantDeltas[2]))

	def _set_formantF2Delta(self, value: str | int) -> None:
		self._setFormantDelta(2, value)

	def _get_formantF3Delta(self) -> str:
		return str(int(self._formantDeltas[3]))

	def _set_formantF3Delta(self, value: str | int) -> None:
		self._setFormantDelta(3, value)

	def _get_formantAlfDelta(self) -> str:
		return str(int(self._formantDeltas[4]))

	def _set_formantAlfDelta(self, value: str | int) -> None:
		self._setFormantDelta(4, value)

	def _get_formantA1Delta(self) -> str:
		return str(int(self._formantDeltas[5]))

	def _set_formantA1Delta(self, value: str | int) -> None:
		self._setFormantDelta(5, value)

	def _get_formantA2Delta(self) -> str:
		return str(int(self._formantDeltas[6]))

	def _set_formantA2Delta(self, value: str | int) -> None:
		self._setFormantDelta(6, value)

	def _get_formantA3Delta(self) -> str:
		return str(int(self._formantDeltas[7]))

	def _set_formantA3Delta(self, value: str | int) -> None:
		self._setFormantDelta(7, value)

	def _get_formantA4Delta(self) -> str:
		return str(int(self._formantDeltas[8]))

	def _set_formantA4Delta(self, value: str | int) -> None:
		self._setFormantDelta(8, value)

	def _get_formantIvDelta(self) -> str:
		return str(int(self._formantDeltas[9]))

	def _set_formantIvDelta(self, value: str | int) -> None:
		self._setFormantDelta(9, value)

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
			return decode_swapped_hex_byte(bytes(digits))

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
		# Some applications call NVDA's speech API repeatedly without cancelling the previous utterance.
		# Apollo has a sizeable internal speech buffer, so this would result in queued speech and poor
		# responsiveness during fast navigation (e.g. pressing Down Arrow while a long message is still
		# being spoken).
		#
		# To keep navigation responsive without breaking typed-character echo, automatically cancel
		# ongoing/queued speech when a new non-trivial utterance arrives.
		try:
			speechSequence = tuple(speechSequence)
		except Exception:
			pass
		else:
			textChars = 0
			shouldAutoCancel = False
			for item in speechSequence:
				if isinstance(item, str):
					textChars += len(item)
					if textChars > 1:
						shouldAutoCancel = True
						break

			if shouldAutoCancel:
				with self._writeStateLock:
					inFlightSpeech = self._isWritingSpeech
				with self._indexLock:
					hasSpeech = self._isSpeaking or bool(self._pendingIndexes)
				if inFlightSpeech or hasSpeech or not self._writeQueue.empty():
					self.cancel()

		# Never block the UI thread on serial I/O. If we're disconnected, queue speech and
		# let the background write thread establish the connection.
		if self._getSerial() is None:
			self._startBackgroundConnect()

		indexes: list[int] = []
		outputParts: list[bytes] = []
		textBufferParts: list[str] = []
		charModeActive = False
		needSpaceBeforeNextText = False
		synthSpellMode = self._spellMode
		pendingPitchBytes: Optional[bytes] = None

		def flushText() -> None:
			nonlocal pendingPitchBytes
			if not textBufferParts:
				return
			text = "".join(textBufferParts)
			textBufferParts.clear()
			if text:
				if pendingPitchBytes is not None:
					outputParts.append(pendingPitchBytes)
					pendingPitchBytes = None
				outputParts.append(
					encode_text(
						text,
						expand_numbers=self._expandNumbers and not (synthSpellMode or charModeActive),
					),
				)

		if self._needsSettingsSync:
			self._queueSettingsSync()

		# Restore base rate at the start of each utterance.
		# Some operations (e.g. @J soft reset during formant tuning) can temporarily reset speed
		# to defaults if the device drops bytes or applies the reset asynchronously.
		outputParts.append(f"@W{self._rate} ".encode("ascii", "ignore"))

		# Restore base pitch at the start of each utterance.
		# Some Apollo firmware variants appear to only apply pitch at phrase boundaries.
		outputParts.append(f"@F{_hexDigit(self._pitch)} ".encode("ascii", "ignore"))

		global _nvdaStartupAnnounced
		if (
			self._announceNvdaStartup
			and not _nvdaStartupAnnounced
			and (time.monotonic() - self._driverInitTime) < _NVDA_STARTUP_ANNOUNCE_WINDOW_SECONDS
		):
			textBufferParts.append("Ładowanie NVDA ")
			_nvdaStartupAnnounced = True

		for item in speechSequence:
			if isinstance(item, str):
				sanitized = sanitize_text(item)
				if needSpaceBeforeNextText:
					if sanitized:
						if not synthSpellMode and sanitized[0].isalnum():
							textBufferParts.append(" ")
						needSpaceBeforeNextText = False
				textBufferParts.append(sanitized)
				if charModeActive:
					# NVDA doesn't always send CharacterModeCommand(False); apply it only to the
					# immediately following text chunk.
					flushText()
					charModeActive = False
					needSpaceBeforeNextText = True
			elif isinstance(item, IndexCommand):
				flushText()
				outputParts.append(self._indexMarkCommand)
				indexes.append(item.index)
			elif isinstance(item, CharacterModeCommand):
				flushText()
				if item.state:
					charModeActive = True
				else:
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
				pendingPitchBytes = f"@F{_hexDigit(apolloPitch)} ".encode("ascii", "ignore")
			elif isinstance(item, EndUtteranceCommand):
				flushText()
				charModeActive = False
				pendingPitchBytes = None
			elif isinstance(item, BreakCommand):
				flushText()
				if item.time and item.time > 0:
					repeats = max(1, round(item.time / 100))
					# Delimit repeated @Tx commands so they don't eat the following text/commands.
					# Avoid literal spaces in spell/character mode (they may be spoken as "space").
					separator = b"" if (synthSpellMode or charModeActive) else b" "
					outputParts.append((b"@Tx" + separator) * repeats)

		if charModeActive:
			flushText()
			charModeActive = False
			needSpaceBeforeNextText = False
		flushText()
		# Always append a final index mark so we can reliably detect end of speech.
		outputParts.append(self._indexMarkCommand)
		indexes.append(_INTERNAL_DONE_INDEX)
		data = b"".join(outputParts) + _CR
		self._queueWrite(data, indexes=tuple(indexes))

	def cancel(self):
		wasSpeaking = False
		hadPendingQueue = False
		inFlightSpeech = False
		with self._indexLock:
			wasSpeaking = self._isSpeaking or bool(self._pendingIndexes)
			hadPendingQueue = not self._writeQueue.empty()
		with self._writeStateLock:
			inFlightSpeech = self._isWritingSpeech

		# Abort any in-flight write item and discard queued speech immediately.
		self._cancelGeneration += 1
		self._clearIndexes()
		ser = self._getSerial()
		preserved: list[Optional[_WriteItem]] = []
		try:
			while True:
				item = self._writeQueue.get_nowait()
				# Keep non-cancelable items (e.g. settings sync / setting commands) so a cancel doesn't
				# desynchronize the synth and cause later "reverts".
				if item is None or (item is not None and not item.cancelable and not item.isMute):
					preserved.append(item)
		except queue.Empty:
			if ser is not None and (wasSpeaking or hadPendingQueue or inFlightSpeech):
				# Ensure the mute reaches the synthesizer quickly even if the UI thread can't acquire the
				# serial lock (e.g. while the write thread is mid-write). This write item clears the OS TX
				# buffer and sends Control+X plus an indexing enable command.
				self._queueWrite(_MUTE + self._indexEnableCommand, cancelable=False, isMute=True)
		for item in preserved:
			self._writeQueue.put(item)

		if ser is not None:
			# Ask pyserial to cancel a potentially blocking speech write (if supported).
			# Don't cancel non-speech writes (settings sync), otherwise we may interrupt an in-flight
			# @J/settings update and leave the synth in a reset/default state.
			if inFlightSpeech:
				try:
					cancelWrite = getattr(ser, "cancel_write", None)
					if callable(cancelWrite):
						cancelWrite()
				except Exception:
					pass

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
