## 0.1.4

- Disable Apollo indexing (`@1?`/`@1+`/`@l+`) to fix an issue where an extra “1” could be spoken at the start of utterances.

## 0.1.3

- Stop cancelling speech and re-sending the full settings prefix on every utterance (fixes choppy speech / random voice changes, improves typing echo).
- Track end-of-speech using a final `@l+` index mark so `synthDoneSpeaking` is reliable.

## 0.1.2

- Changed add-on ID to `apollo2` so it can be installed alongside older Apollo add-ons.
- Disabled `CharacterModeCommand` and `LangChangeCommand` handling to prevent unintended spell mode / ROM switching.

## 0.1.1

- Renamed synth driver module to `apollo2` so it can coexist with older Apollo add-ons.

## 0.1.0

- Initial public repository + NVDA add-on build scaffolding.
- Expanded command support (ROM, punctuation/spell/hyper/phonetic, mark-space ratio) and NVDA speech commands (break/prosody).
- Added `@L` ROM/language discovery (used to label ROM slots) and basic `LangChangeCommand` -> ROM switching.
