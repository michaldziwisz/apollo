# Apollo NVDA add-on (synth driver)

NVDA synthesizer driver for Dolphin Apollo 2 / PC 2 Card / Juno (serial).

## Status

Work in progress: modernizing the add-on and implementing missing command support.

## Build (from source)

This repository uses the standard NVDA add-on SCons template.

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip wheel
.venv/bin/pip install scons markdown
.venv/bin/scons
```

The build produces `apollo2-<version>.nvda-addon` in the repository root.

## Settings mapping

- `Voice` -> `@Vd`
- `Speaker table` -> `@Kb`
- `Voice source/filter` -> `@$o`
- `ROM slot` -> `@=d,`
- `Rate` -> `@Wd`
- `Pitch` -> `@Fh`
- `Volume` -> `@Ah`
- `Inflection` (prosody) -> `@Ro`
- `Punctuation` -> `@Pb`
- `Spell mode` -> `@Sb`
- `Hypermode` -> `@Hb`
- `Phonetic mode` -> `@Xb`
- `Mark-space ratio` -> `@Mhh`
- `Word pause` -> `@Qd`
- `Sentence pause` -> `@Dh`
- `Voicing` -> `@Bd`

Indexing uses Apollo `@I?` and `@I+` commands (required for continuous reading / “Say All”).

`BreakCommand` maps to `@Tx` (~100 ms pause).

Serial settings:

- `Serial port` / `Serial baud rate` set the host COM port parameters.
- `Apply baud rate to synthesizer now (@Y)` attempts a one-shot `@Y` switch on the device (may fail on some firmware/USB adapters).

To avoid getting stuck with no speech when the port is misconfigured, the add-on fails fast when Apollo isn't detected during
synth switch/startup. You can configure the serial connection without switching synthesizers via the global command:

- `NVDA+Shift+P`: “Apollo 2: Serial connection…”
  - Use the “Test connection” button to verify the selected port/baud before switching.
- `NVDA+Shift+A`: switch synthesizer to Apollo 2 (only if the device is detected).

## ROM/language detection

The driver queries the synthesizer using `@L` and uses the returned slot info to label ROM slots in NVDA settings.
Automatic ROM switching via `LangChangeCommand` is currently disabled (to avoid regressions).

## Coexistence with older add-ons

This add-on uses a separate add-on ID (`apollo2`) and registers the synthesizer driver as `apollo2`
(shown as “Dolphin Apollo 2 (modern)”), so it should not overwrite or conflict with older Apollo add-ons.

## Notes

The original Dolphin manual is not included in this repository (copyright).

If your Apollo is connected via a COM port and you don't use a braille display, consider setting NVDA's braille
display to “No braille” (and disabling braille auto-detection). Otherwise, NVDA may repeatedly probe COM ports via
braille display drivers, which can lead to `PermissionError` spam in `nvda.log` and occasional responsiveness issues.

The add-on also provides:

- `NVDA+Shift+B`: disable braille auto-detection (switch braille display to “No braille”).
