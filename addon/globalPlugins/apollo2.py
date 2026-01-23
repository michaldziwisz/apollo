from __future__ import annotations

import time
from typing import Optional

import addonHandler

addonHandler.initTranslation()

try:
	import config  # type: ignore[import-not-found]
	import gui  # type: ignore[import-not-found]
	import ui  # type: ignore[import-not-found]
	import wx  # type: ignore[import-not-found]
	import globalPluginHandler  # type: ignore[import-not-found]
	import synthDriverHandler  # type: ignore[import-not-found]
except ImportError:
	# Not running under NVDA.
	config = None  # type: ignore[assignment]
	gui = None  # type: ignore[assignment]
	ui = None  # type: ignore[assignment]
	wx = None  # type: ignore[assignment]
	globalPluginHandler = object  # type: ignore[assignment]
	synthDriverHandler = None  # type: ignore[assignment]


_SYNTH_NAME = "apollo2"
_AUTO_PORT = "auto"
# Apollo is most stable (and power-up defaults) at 9600 baud. We intentionally only support 9600
# to avoid probing/operating at other rates which can cause false negatives and unstable behavior.
_SUPPORTED_BAUD_RATES = (9600,)
_NO_BRAILLE = "noBraille"
_INDEX_QUERY_COMMANDS = (b"@I?", b"@1?")
_INDEX_RESPONSE_PREFIX = b"I"
_MUTE = b"\x18"
_NAK = b"\x15"


def _getComPorts() -> list[tuple[str, str]]:
	ports: list[tuple[str, str]] = [(_AUTO_PORT, _("Auto (detect)"))]
	if config is None:
		return ports
	try:
		try:
			from serial.tools import list_ports  # type: ignore[import-not-found]
		except ImportError:
			list_ports = None  # type: ignore[assignment]
		if list_ports is None:
			return ports
		for p in list_ports.comports():
			device = getattr(p, "device", None)
			if not device:
				continue
			description = getattr(p, "description", "") or ""
			label = f"{device} - {description}" if description else device
			ports.append((device, label))
	except Exception:
		pass
	return ports


def _importSerial():
	try:
		import serial  # type: ignore[import-not-found]

		return serial
	except Exception:
		pass
	try:
		from synthDrivers.apollo2 import cserial as serial  # type: ignore[import-not-found]

		return serial
	except Exception:
		return None


def _configureSerialForApollo(ser) -> None:
	"""Best-effort serial configuration matching the synth driver's expectations."""
	# Apollo uses RTS as a direction/handshake line for 2-way comms. Use pyserial RS485 mode
	# (RTS high for TX, low for RX) so the device can reply to @c? queries.
	try:
		try:
			from serial import rs485  # type: ignore[import-not-found]
		except Exception:
			from synthDrivers.apollo2.cserial import rs485  # type: ignore[import-not-found,no-redef]
		try:
			ser.rs485_mode = rs485.RS485Settings()
		except Exception:
			pass
	except Exception:
		pass

	try:
		ser.dsrdtr = False
	except Exception:
		pass
	try:
		ser.dtr = True
	except Exception:
		pass
	try:
		ser.reset_input_buffer()
		ser.reset_output_buffer()
	except Exception:
		pass


def _probeApolloIndexResponse(ser, *, command: bytes, timeout: float = 0.35) -> bool:
	"""Best-effort probe that matches Apollo indexing response shape (IabT/M)."""
	try:
		ser.reset_input_buffer()
	except Exception:
		pass
	try:
		ser.write(_MUTE)
		try:
			ser.flush()
		except Exception:
			pass
	except Exception:
		return False
	try:
		# Include a delimiter (space) after the query; some firmware variants require it.
		ser.write(command + b" ")
		try:
			ser.flush()
		except Exception:
			pass
	except Exception:
		return False
	deadline = time.monotonic() + max(0.05, float(timeout))
	while time.monotonic() < deadline:
		try:
			first = ser.read(1)
		except Exception:
			return False
		if not first or first == _NAK:
			continue
		if first != _INDEX_RESPONSE_PREFIX:
			continue
		try:
			rest = ser.read(3)
		except Exception:
			return False
		if len(rest) != 3:
			return False
		hexDigits = b"0123456789abcdefABCDEF"
		if rest[0:1] not in hexDigits or rest[1:2] not in hexDigits:
			return False
		if rest[2:3] not in (b"T", b"M", b"t", b"m"):
			return False
		return True
	return False


def _testApolloConnection(*, port: str, baud: int) -> tuple[bool, Optional[str]]:
	serial = _importSerial()
	if serial is None:
		return False, None
	baudRate = 9600

	candidatePorts: list[str] = []
	port = (port or "").strip() or _AUTO_PORT
	if port != _AUTO_PORT:
		candidatePorts = [port]
	else:
		candidatePorts = [p for p, _ in _getComPorts() if p != _AUTO_PORT]

	for candidate in candidatePorts:
		try:
			ser = serial.serial_for_url(  # type: ignore[attr-defined]
				candidate,
				baudrate=baudRate,
				timeout=0.1,
				write_timeout=0.3,
				rtscts=False,
				dsrdtr=False,
				xonxoff=False,
			)
		except Exception:
			continue
		try:
			_configureSerialForApollo(ser)
			for cmd in _INDEX_QUERY_COMMANDS:
				if _probeApolloIndexResponse(ser, command=cmd, timeout=0.35):
					return True, candidate
		finally:
			try:
				ser.close()
			except Exception:
				pass
	return False, None


def _getSpeechSection():
	if config is None:
		return None
	try:
		return config.conf.get("speech")
	except Exception:
		return None


def _ensureSpeechSynthSection():
	if config is None:
		return None
	speechSection = _getSpeechSection()
	if speechSection is None:
		try:
			config.conf["speech"] = {}
			speechSection = config.conf.get("speech")
		except Exception:
			return None
	if speechSection is None:
		return None
	driverSection = speechSection.get(_SYNTH_NAME)
	if driverSection is None:
		try:
			speechSection[_SYNTH_NAME] = {}
		except Exception:
			pass
		driverSection = speechSection.get(_SYNTH_NAME)
	return driverSection


def _readCurrentPortAndBaud() -> tuple[str, int]:
	port = _AUTO_PORT
	baud = 9600
	driverSection = _ensureSpeechSynthSection()
	if driverSection is None:
		return port, baud
	try:
		port = str(driverSection.get("port") or _AUTO_PORT).strip() or _AUTO_PORT
	except Exception:
		port = _AUTO_PORT
	baud = 9600
	return port, baud


def _writePortAndBaud(*, port: str, baud: int) -> bool:
	driverSection = _ensureSpeechSynthSection()
	if driverSection is None:
		return False
	try:
		driverSection["port"] = (port or "").strip() or _AUTO_PORT
		driverSection["baudRate"] = "9600"
		try:
			config.conf.save()
		except Exception:
			pass
		return True
	except Exception:
		return False


def _setNoBraille() -> bool:
	"""Disable braille auto-detection by switching braille display to 'noBraille'."""
	if config is None:
		return False
	try:
		brailleSection = config.conf.get("braille") if hasattr(config, "conf") else None
		if brailleSection is None:
			config.conf["braille"] = {}
			brailleSection = config.conf.get("braille")
		if brailleSection is None:
			return False
		brailleSection["display"] = _NO_BRAILLE
		try:
			config.conf.save()
		except Exception:
			pass

		# Apply immediately if possible (best effort, API differs by NVDA version).
		try:
			import braille  # type: ignore[import-not-found]

			handler = getattr(braille, "handler", None)
			if handler is not None:
				for name in ("setDisplayByName", "setDisplay", "_switchDisplay"):
					fn = getattr(handler, name, None)
					if callable(fn):
						try:
							fn(_NO_BRAILLE)
							break
						except Exception:
							pass
		except Exception:
			pass
		return True
	except Exception:
		return False


if wx is not None and gui is not None and config is not None:

	class _SerialConfigDialog(wx.Dialog):  # type: ignore[misc]
		def __init__(self, parent):
			super().__init__(parent, title=_("Apollo 2: Serial connection"))

			currentPort, currentBaud = _readCurrentPortAndBaud()
			portChoices = _getComPorts()
			portLabels = [label for _, label in portChoices]
			self._portValues = [value for value, _ in portChoices]

			self._portChoice = wx.Choice(self, choices=portLabels)
			try:
				self._portChoice.SetSelection(self._portValues.index(currentPort))
			except Exception:
				self._portChoice.SetSelection(0)

			baudLabels = [str(b) for b in _SUPPORTED_BAUD_RATES]
			self._baudLabel = wx.StaticText(self, label=baudLabels[0] if baudLabels else "9600")

			sizer = wx.BoxSizer(wx.VERTICAL)
			form = wx.FlexGridSizer(cols=2, hgap=10, vgap=8)
			form.AddGrowableCol(1, 1)

			form.Add(wx.StaticText(self, label=_("Serial port:")), 0, wx.ALIGN_CENTER_VERTICAL)
			form.Add(self._portChoice, 1, wx.EXPAND)

			form.Add(wx.StaticText(self, label=_("Baud rate:")), 0, wx.ALIGN_CENTER_VERTICAL)
			form.Add(self._baudLabel, 0, wx.ALIGN_CENTER_VERTICAL)

			sizer.Add(form, 1, wx.ALL | wx.EXPAND, 12)

			self._disableBrailleAutoDetectCheck = wx.CheckBox(
				self,
				label=_("Disable braille auto-detection (set display to No braille)"),
			)
			sizer.Add(self._disableBrailleAutoDetectCheck, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

			self._testButton = wx.Button(self, label=_("Test connection"))

			buttons = wx.StdDialogButtonSizer()
			buttons.AddButton(self._testButton)
			buttons.AddButton(wx.Button(self, wx.ID_OK))
			buttons.AddButton(wx.Button(self, wx.ID_CANCEL))
			buttons.Realize()
			sizer.Add(buttons, 0, wx.ALL | wx.EXPAND, 12)
			self.SetSizerAndFit(sizer)

			self._testButton.Bind(wx.EVT_BUTTON, self._onTest)

		def _onTest(self, evt):
			port, baud, _ = self.getValues()
			ok, detectedPort = _testApolloConnection(port=port, baud=baud)
			if ok:
				if detectedPort and detectedPort in self._portValues:
					try:
						self._portChoice.SetSelection(self._portValues.index(detectedPort))
					except Exception:
						pass
				try:
					ui.message(_("Apollo detected."))
				except Exception:
					pass
			else:
				try:
					ui.message(_("Apollo not detected on the selected port/baud."))
				except Exception:
					pass

		def getValues(self) -> tuple[str, int, bool]:
			port = _AUTO_PORT
			baud = 9600
			disableBrailleAutoDetect = False
			try:
				port = self._portValues[self._portChoice.GetSelection()]
			except Exception:
				port = _AUTO_PORT
			try:
				disableBrailleAutoDetect = bool(self._disableBrailleAutoDetectCheck.GetValue())
			except Exception:
				disableBrailleAutoDetect = False
			return port, baud, disableBrailleAutoDetect


	class GlobalPlugin(globalPluginHandler.GlobalPlugin):  # type: ignore[misc]
		scriptCategory = _("Apollo 2")

		def __init__(self):
			super().__init__()
			self._menuItem: Optional[int] = None
			try:
				tray = gui.mainFrame.sysTrayIcon  # type: ignore[union-attr]
				menu = getattr(tray, "preferencesMenu", None) or getattr(tray, "toolsMenu", None)
				if menu is not None:
					self._menuItem = menu.Append(wx.ID_ANY, _("Apollo 2: Serial connection…")).GetId()
					tray.Bind(wx.EVT_MENU, self._onMenu, id=self._menuItem)
			except Exception:
				self._menuItem = None

		def terminate(self):
			try:
				if self._menuItem is not None:
					tray = gui.mainFrame.sysTrayIcon  # type: ignore[union-attr]
					menu = getattr(tray, "preferencesMenu", None) or getattr(tray, "toolsMenu", None)
					if menu is not None:
						menu.Remove(self._menuItem)
			except Exception:
				pass
			super().terminate()

		def _onMenu(self, evt):
			self.script_configureSerial(None)

		def script_disableBrailleAutoDetect(self, gesture):
			if _setNoBraille():
				try:
					ui.message(_("Braille auto-detection disabled (No braille)."))
				except Exception:
					pass
			else:
				try:
					ui.message(_("Failed to change braille settings."))
				except Exception:
					pass

		def script_configureSerial(self, gesture):
			parent = getattr(gui, "mainFrame", None)
			dlg = _SerialConfigDialog(parent)
			try:
				if dlg.ShowModal() != wx.ID_OK:
					return
				port, baud, disableBrailleAutoDetect = dlg.getValues()
			finally:
				try:
					dlg.Destroy()
				except Exception:
					pass

			if _writePortAndBaud(port=port, baud=baud):
				try:
					ui.message(_("Apollo 2 settings saved."))
				except Exception:
					pass
			else:
				try:
					ui.message(_("Failed to save Apollo 2 settings."))
				except Exception:
					pass

			if disableBrailleAutoDetect:
				self.script_disableBrailleAutoDetect(None)

		def script_switchToApollo2(self, gesture):
			if synthDriverHandler is None:
				return
			port, baud = _readCurrentPortAndBaud()
			ok, _detected = _testApolloConnection(port=port, baud=baud)
			if not ok:
				try:
					ui.message(_("Apollo not detected; not switching synthesizer."))
				except Exception:
					pass
				return
			try:
				synthDriverHandler.setSynth(_SYNTH_NAME)
				try:
					ui.message(_("Apollo 2 active."))
				except Exception:
					pass
			except Exception:
				try:
					ui.message(_("Failed to switch synthesizer to Apollo 2."))
				except Exception:
					pass

		__gestures = {
			"kb:NVDA+shift+p": "configureSerial",
			"kb:NVDA+shift+b": "disableBrailleAutoDetect",
			"kb:NVDA+shift+a": "switchToApollo2",
		}

else:

	class GlobalPlugin:  # type: ignore[misc]
		pass
