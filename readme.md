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

The build produces `apollo-<version>.nvda-addon` in the repository root.

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

Indexing uses `@1+`, `@1?` and `@l+` (see the Dolphin Series 2 manual).

`BreakCommand` maps to `@Tx` (~100 ms pause).

## ROM/language detection

The driver queries the synthesizer using `@L` and uses the returned slot info to label ROM slots in NVDA settings.
`LangChangeCommand` attempts to switch ROM slots when possible (based on `@L` data).

## Coexistence with older add-ons

This add-on registers the synthesizer driver as `apollo2` (shown as “Dolphin Apollo 2 (modern)”), so it should not conflict
with older Apollo add-ons that provide a driver named `apollo`.

## Notes

The original Dolphin manual is not included in this repository (copyright).
