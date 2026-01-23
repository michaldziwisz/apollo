## 0.1.92

- Improve cancellation reliability (Control+X) during fast navigation by always sending a mute command on cancel.

## 0.1.91

- Fix a regression where automatic cancellation of queued speech didn’t trigger, causing lag during fast navigation.

## 0.1.90

- Fix occasional formant tuning glitches during rapid navigation by avoiding cancellation of in-flight settings writes.

## 0.1.89

- Fix occasional speed/rate resets while tuning formants by re-applying the current rate at the start of each utterance.

## 0.1.88

- Fix a regression where Apollo could connect successfully but speech output was dropped due to a write-loop bug.

## 0.1.87

- Fix connection detection on some hardware by probing Apollo via indexing response (instead of `@V?`).

## 0.1.86

- Force Apollo serial baud rate to 9600 for stability (no probing/operation at other rates).
- Improve serial handshake (RS485/RTS toggling) so Apollo can reply to probe commands more reliably.

## 0.1.85

- Fix a regression where a bad indent prevented baud-rate probing during connection detection (causing immediate “Apollo not detected” failures).

## 0.1.84

- Fix probing on some firmware variants by adding required delimiters (trailing space) to `@V?` / `@I?` / `@1?` queries.

## 0.1.83

- Fall back to Apollo indexing command variant `@1?` / `@1+` if the default `@I?` / `@I+` probe doesn't respond.

## 0.1.82

- Improve connection detection: probe Apollo with a lightweight `@V?` query before relying on indexing responses.
- Improve baud-rate probing order in the bounded startup check (faster detection when the configured baud rate doesn't match the synth).

## 0.1.81

- Fix a regression where `cancel()` was accidentally nested inside `speak()`, breaking interruption and sometimes causing synth errors.
- Improve `_ensureConnected(maxDuration=...)` so busy/locked COM ports (permission denied) are retried briefly within the allowed budget.
- Avoid starting background connection attempts / serial writes while NVDA is applying profile settings (prevents COM port grabs on failed synth switch).

## 0.1.80

- Fix intermittent cancel during fast navigation by tracking in-flight speech writes (so mute isn't skipped when the write queue is empty but a write is still in progress).

## 0.1.79

- Improve interruption reliability: queue a dedicated mute write item so Control+X reaches the device quickly even during in-flight writes.

## 0.1.78

- Fix a regression in serial connection probing (auto port / baud switching helpers).
- Add a “Test connection” button and `NVDA+Shift+A` action to safely switch to Apollo 2 after verifying the port.

## 0.1.77

- Fix startup regression where the driver failed to load under NVDA (`_AUTO_PORT` constant scope).

## 0.1.76

- Improve interruption: when a new non-trivial utterance arrives while Apollo is still speaking/queued,
  the driver cancels the previous speech so navigation commands interrupt immediately.

## 0.1.75

- Make `Serial port` default to `Auto (detect)` for safer first-time setup.
- Fail fast on synth switch if Apollo isn't detected (prevents NVDA going silent on a wrong port).
- Add a global "Apollo 2: Serial connection" dialog (NVDA+Shift+P) to change port/baud without switching synths.
- Add an optional checkbox/action to disable braille auto-detection (set display to "No braille") to avoid COM port probing conflicts.

## 0.1.74

- No functional changes; internal refactor and new unit tests/CI.

## 0.1.73

- No functional changes; fixes the GitHub release workflow packaging.

## 0.1.72

- Improve responsiveness by cancelling in-flight writes (when supported by pyserial) and using smaller serial write chunks.

## 0.1.71

- Reduce freezes on flaky serial ports by backing off after write timeouts and making cancel non-blocking.

## 0.1.70

- Disable serial port flow control by default to avoid write timeouts on some on-board COM ports.

## 0.1.69

- Replace auto `@Y` baud-rate switching with a one-shot “apply now” action to avoid startup delays.

## 0.1.68

- Reduce startup delays when `@Y` baud-rate switching fails by limiting the handshake time.

## 0.1.67

- Try additional `@Y` command formats and sync variants to improve baud-rate switching on different firmware.

## 0.1.66

- Improve `@Y` baud-rate switching (mute before switch + extra timing margin).
- Avoid repeating a failing `@Y` attempt on every reconnect (reduces startup delays).

## 0.1.65

- Add an option to apply the configured baud rate to the synthesizer using `@Y` (off by default, for safety).

## 0.1.64

- Fix repeated “1” announcements by reverting indexing commands to `@I?` / `@I+` (avoids the problematic `@1?` / `@1+` variant).

## 0.1.63

- Fix stray “1” announcements on some firmware by using the correct indexing mark command when @1? probing is used.

## 0.1.62

- Improve serial connection reliability (more robust indexing probe + auto-detect @I? vs @1?).
- Prefer Apollo's power-up default baud rate (9600) when indexing can't be verified (avoids garbled output if a non-working baud is configured).
- Avoid running number-to-words expansion when there are no digits in the text (reduces lag).

## 0.1.61

- Fix baud-rate switching on Windows by changing the host baud rate in-place (instead of reopening the COM port immediately after `@Y`).

## 0.1.60

- Fix baud-rate switching so Apollo actually reconnects at the configured speed (prevents falling back to 9600 after attempting `@Y`).

## 0.1.59

- Fix a regression where speech could break after settings sync (flushText scope bug).
- Reintroduce the serial baud rate setting and apply it to Apollo using `@Y` (reduces lag during fast navigation).

## 0.1.58

- Improve responsiveness when navigating lists by disabling number-to-words expansion by default (can be enabled in settings).

## 0.1.57

- Improve responsiveness when changing advanced formant tuning values quickly (debounce repeated settings syncs).

## 0.1.56

- Improve responsiveness of the Speech settings dialog by using a smaller default formant tuning range (±50).
- Add an advanced option to switch the formant tuning range to the full documented ±255.

## 0.1.55

- Fix a regression where the driver could stop speaking due to a broken settings sync/write loop.
- Fix voice / speaker table / voice filter changes not being applied.
- Make serial disconnect safer (avoid closing the port while another thread is writing).

## 0.1.54

- Fix advanced formant tuning consistency by avoiding a race that could mix full reset-based updates with incremental @u diffs.

## 0.1.53

- Fix “Synthesizer error” on startup (regression in 0.1.52).

## 0.1.52

- Fix inconsistent formant tuning by reapplying it deterministically (soft reset + full settings sync per change).

## 0.1.51

- Expose full documented range for advanced formant tuning (-255..255).

## 0.1.50

- Fix non-deterministic formant tuning by preventing speech cancel from aborting in-flight serial writes (@u/@J).

## 0.1.49

- Fix non-deterministic formant tuning by flushing non-cancelable setting writes (prevents cancel() from dropping @u/@J commands from the OS TX buffer).

## 0.1.48

- Make advanced formant tuning apply immediately (removes the debounce that caused inconsistent results).
- Fix “Reset formant tuning to defaults” to always force a soft reset (@J) so hardware really returns to defaults.

## 0.1.47

- Replace advanced formant tuning sliders (0–100) with real-value lists (-50..50, default 0).
- Ensure voice / speaker table / filter changes re-sync deterministically when formant tuning is active.

## 0.1.46

- Replace Apollo-specific sliders (mark-space ratio, sentence pause, word pause, voicing) with real-value lists.
- Tune the slider step sizes for rate/pitch/volume/inflection to better match Apollo’s discrete ranges.

## 0.1.43

- Fix a regression where apollo2 failed to load (synthesizer error) after 0.1.42.

## 0.1.44

- Debounce advanced formant tuning updates to reduce lag when adjusting sliders.

## 0.1.45

- Fix formant slider behavior (avoid needing multiple arrow presses before the displayed value changes).

## 0.1.42

- Fix formant tuning stability by applying @u diffs during settings sync (no @J unless reconnect).
- Coalesce formant slider changes to avoid flooding the serial queue and freezing speech.

## 0.1.41

- Make formant tuning smooth by applying @u deltas incrementally (no @J soft reset per slider change).
- Add an advanced “Reset formant tuning to defaults” action.

## 0.1.40

- Fix settings “reverting” / random voice changes by making Apollo settings sync non-cancelable (decoupled from speech).

## 0.1.39

- Fix applying advanced formant tuning (send @J separately so settings aren’t dropped).

## 0.1.38

- Fix resetting formant tweaks back to defaults when deltas are set to 0.

## 0.1.37

- Add advanced formant tuning settings (@u… +/-).

## 0.1.36

- Fix a regression where apollo2 could fail to load in NVDA.
- Ensure voice filter is applied at startup (ordering fix in the settings prefix).

## 0.1.35

- Apply Apollo profile settings at startup (voice filter no longer requires opening Voice Settings).

## 0.1.34

- Sync Apollo voice settings at startup (e.g. voice filter applies without opening Voice Settings).

## 0.1.33

- Remove the experimental serial baud rate setting (Apollo stays at 9600 baud).

## 0.1.32

- Fix a regression where failed baud switching could leave the serial port at the wrong speed (causing gibberish speech).

## 0.1.31

- Avoid long startup stalls by moving @Y baud switching to a background thread.

## 0.1.30

- Improve @Y baud rate switching reliability (try space-separated formats from the manual).
- Add debug logs for @Y switching attempts (useful in nvda.log).

## 0.1.29

- Improve @Y baud rate switching reliability (tries multiple sync sequences).
- Log the effective port/baud on connect.

## 0.1.28

- Fix @Y baud rate switching (prevents sync errors / fallback to 9600).

## 0.1.27

- Improve serial port auto-detection and avoid silence caused by strict probing.

## 0.1.26

- Fix a regression where Apollo could go silent due to overly strict connection probing.

## 0.1.25

- Avoid blocking the NVDA UI on serial connection attempts (prevents freezes when the port is busy).
- Add an "Auto (detect)" serial port option.

## 0.1.24

- Fix opening Voice Settings when the experimental baud rate setting is present.

## 0.1.23

- Fix a regression where opening Voice Settings could be blocked by serial probing.

## 0.1.22

- Fix a regression where opening Voice Settings could hang the UI (serial connect moved to background).

## 0.1.21

- Add an experimental serial baud rate setting (up to 57600) to improve responsiveness.

## 0.1.20

- Fix NVDA character descriptions during cursor movement (ensure the 1s pause command doesn’t swallow the description).

## 0.1.19

- Restore an NVDA startup announcement (“Ładowanie NVDA”) as a one-time prefix on the first spoken utterance.
- Fix capitals pitch change by separating pitch commands from text and avoiding end-of-utterance pitch resets (base pitch is restored at the start of each utterance).

## 0.1.18

- Buffer speech for a short time when the serial port is temporarily busy, so the NVDA startup message isn't lost.
- Don't expand digits into words while in spell mode (prevents "1 j e d e n" type spelling).

## 0.1.17

- Fix sporadic speech dropouts (e.g. Alt+Tab not announced) after NVDA profile switches by opening the serial port immediately on synth load.

## 0.1.16

- Fix capitals pitch change by applying `PitchCommand` immediately before the next text chunk (works with `CharacterModeCommand`).
- Retry opening the serial port briefly so the NVDA startup message is less likely to be missed.

## 0.1.15

- Fix regression where Apollo could speak 'espa/spacja' during typing echo (spell mode commands no longer emit literal spaces).

## 0.1.14

- Handle `CharacterModeCommand` so shortcuts like “Alt+ s zaznaczone” don’t collapse into “Alt+szaznaczone”.
- Improve `PitchCommand` handling (used for raising pitch on capitals).

## 0.1.13

- Implement `PitchCommand` so NVDA’s “cap pitch change” (raising pitch on capitals) works.

## 0.1.12

- Normalize whitespace/control characters in spoken text to prevent word-joining issues (e.g. “s zaznaczone” being spoken as “szaznaczone”).

## 0.1.11

- Fix a stray “t” being spoken at NVDA startup by sending ROM selection separately (not embedded in speech).

## 0.1.10

- Fix regression in 0.1.9 where the driver could stop speaking due to `_settingsPrefix` being indented incorrectly.

## 0.1.9

- Attempt to fix a stray “1” being spoken at NVDA startup by selecting the default ROM using `@=T` instead of `@=1` (superseded by 0.1.11).

## 0.1.8

- Fix delayed speech stopping (Ctrl) by purging the serial output buffer on cancel.

## 0.1.7

- Fix continuous reading (“Say All”) by fixing a startup crash in the indexing poll thread.

## 0.1.6

- Fix continuous reading (“Say All”) by decoding the Apollo index counter in a way that works across firmware variants.

## 0.1.5

- Fix “Say All” / continuous reading by re-enabling indexing, using Apollo’s `@I?` / `@I+` commands (instead of `@1?` / `@1+`).

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
