# Streaming transcription — decode while the microphone is still open

Date: 2026-09-08
Status: approved design, not yet implemented

## The problem

A dictation is transcribed only after it has ended. `_record_and_enqueue`
blocks inside `audio.record_until_stop` until the key is released, the silence
timer fires or `max_recording_time` is reached, and only then is a single
`TranscriptionJob` queued. The decode therefore starts at the moment the user
has finished speaking and wants the text.

The wait that follows scales with the recording. A five-minute dictation —
`max_recording_time` is 300 s, and that ceiling is reached in practice — buys
a five-minute clip's worth of waiting with nothing on screen but a spinner,
while the GPU sat idle for those same five minutes with the microphone open.

The goal: run the decode during capture, so that releasing the key leaves at
most one segment outstanding. The delivered text must not change.

## Why segmenting is safe here

Cutting audio into pieces normally costs accuracy, because a decoder that sees
less context guesses worse. That argument does not apply to this codebase, for
a reason already in the source.

`DECODE_OPTIONS` sets `condition_on_previous_text=False` (transcribe.py), so
Whisper never feeds its own output back in. It already decodes each 30-second
window independently — that setting exists because conditioning sent long
dictations into repetition loops. **There is no cross-window textual context
for segmentation to take away.** The vocabulary bias from `initial_prompt.md`
is passed per call, so every segment keeps it.

Two real risks remain, and the design addresses both:

1. **A word cut in half.** Avoided by cutting only during silence — the
   recorder already computes per-chunk RMS against `silence_threshold`.
2. **Padding hallucination.** Whisper pads every call out to 30 seconds, and a
   short segment that is mostly padding is exactly where it invents stock
   phrases. Avoided by refusing to cut before a segment holds a full window's
   worth of audio.

## Non-goals

- **Typing as you speak.** Text still reaches the target application once,
  when the dictation is complete. Progressive delivery would change what
  cancel, undo, Enter suppression and a mid-dictation focus change mean; none
  of that is being reopened here.
- **A different decoder.** No faster-whisper, no hosted realtime API. Same
  model, same options; the only change is *when* it runs.
- **Surviving a crash mid-recording.** Today a process death before capture
  ends loses the recording, because the spool is written afterwards. That
  stays true. Streaming neither worsens it nor fixes it.

## Design

### `audio.SegmentCutter`

The cutting decision, isolated from everything else. It is fed the
`(bytes, rms)` pair the capture loop already computes and answers one
question: close the segment here, or keep going. It touches neither PortAudio
nor Whisper, so it is testable from a synthetic level sequence with no
microphone and no GPU.

This is the one quality-critical decision in the feature, which is why it gets
its own unit instead of living inside the capture loop.

**The rule.** A segment closes when both hold:

1. At least `segment_min_seconds` (30) of audio has accumulated, and some of
   it was speech.
2. The level has been below `silence_threshold` for at least
   `segment_cut_silence` (0.5 s).

The 30-second floor is what keeps every call at least one full Whisper window
wide, so no segment is mostly padding.

If `segment_max_seconds` (120) passes without a qualifying pause, the cutter
still does not cut blind. From the moment the minimum is reached it tracks the
quietest candidate it has seen, and at the ceiling it cuts there — a local
minimum, never the middle of a vowel. Natural speech does not reach the
ceiling; the fallback exists so that a pathological case degrades gracefully
instead of mid-word.

**A recording shorter than the minimum produces exactly one segment**, which
is the behaviour that exists today. The common one-sentence dictation becomes
neither faster nor riskier.

### `jobs.DictationSession`

Collects the numbered partial transcripts of one dictation: `add(index,
text)`, `set_final(count)`, `complete()`, `text()`, `cancel()`. It knows when
the whole dictation is assembled and produces the joined result. It knows
nothing about the engine or the UI.

It also carries the dictation's `spool_path`, because the spool entry covers
the whole clip and is dropped once the whole transcript is in history.

### `TranscriptionJob`

Two new fields: `session` and `segment_index`. A job with `session is None`
behaves exactly as it does today, which is what keeps spool recovery
(`_recover_spool`) and the benchmark path unchanged.

### Capture and flow

`record_until_stop` gains an `on_segment` callback. Without it the current
contract is untouched. With it, the recorder hands over each closed segment as
it happens, and hands over whatever is left as the final segment when capture
ends — while still accumulating every byte, so `Capture.data` remains the
complete clip.

That last point matters: the spool keeps writing one WAV per dictation, there
is still one recovery path, and the crash story is unchanged. The cost is
holding the audio twice — once as the whole clip, once split across the
segments still in flight. At 16 kHz mono a five-minute dictation is under
10 MB either way, so this buys crash durability for nothing that matters.

`_record_and_enqueue` submits a partial job per segment onto the existing
queue. The worker decodes them in turn, during capture. When a partial
finishes, its text goes into the session and the overlay updates — and the
worker stops there: no history entry, no typing. When the final segment lands,
one history entry is written from the joined text, the spool entry is dropped,
and the text is delivered through the existing `_deliver` path.

One worker thread and a FIFO queue already guarantee order. The session
assembles by index anyway, so that a future parallel worker cannot corrupt it.

### Queue visibility

`JobQueue` keeps tracking every partial internally, so `busy()` and the
shutdown wait stay correct. But `active()` and `active_count()` return one
entry per session.

Without this the overlay would misbehave visibly: it opens a queue table
whenever `active_count() > 1` (tk_ui.py), so an eight-segment dictation would
grow an eight-row queue panel in the middle of one sentence.

## Error handling

**A session is never merely abandoned.** `_record_and_enqueue` has several
paths that return without producing a transcript — discarded, nothing heard,
benchmark armed, capture raised. Each of them queues no final segment, so the
session can never complete, so its partials would sit in `_active` forever —
`busy()` would stay true and `quit()` would wait on a dictation that is never
coming. Every such path must therefore cancel the session, and cancelling a
session cancels each of its partials through the existing `JobQueue.cancel`,
which retires the waiting ones and lets the worker retire the one in flight.

**Discard during recording.** `stop_recording(discard=True)` cancels the
session; the worker skips its queued partials the way it already skips
`CANCELLED` jobs. Partial text already decoded is dropped with the session.
Externally identical to today: nothing typed, nothing in history.

**Cancel from the overlay.** The visible row is a session representative, so
`cancel_job` cancels the whole session. Typing half a dictation would be worse
than typing none of it.

**Decoder crash.** `RemoteWhisperEngine.transcribe` already restarts the child
process and retries the clip exactly once. Segments inherit that unchanged,
and benefit: re-decoding 30 seconds is cheaper than re-decoding five minutes.

A partial that fails twice fails its session: one `[transcription failed: …]`
history entry, the spool entry deliberately kept, and the next launch recovers
and re-decodes the **whole** clip. No half transcript is ever written to
history — the spool holds the complete audio, so recovery yields the complete
text, and a truncated entry that looks finished is worse than an honest
failure line.

**Quit during recording.** Same answer. Decoded partials are lost, the clip is
in the spool, the next launch produces the full text. That is already the
behaviour today; streaming only makes some work redundant, not some data lost.

**Nothing heard.** The cutter never closes a segment that contained no speech,
so a silent recording produces no partials at all — it arrives as one final
segment and meets the existing `min_speech_seconds` check, in its existing
place. Streaming does not engage for a silent microphone and does not spend
GPU on one.

**Superseded recording.** Each recording owns its session, delivers to its own
captured target, and the `send_enter` rule is unchanged. No new case.

## Overlay

The waveform stays during recording — it is the only feedback showing that the
microphone is live, and replacing it with text would be a bad trade. The text
assembled so far runs on a single line beneath it. After the key is released
the overlay moves to the existing "transcribing" state, showing the full text
so far while the last segment decodes.

Exact placement is settled against `_draw_stage` during implementation. This
document specifies the behaviour, not the pixels.

## Settings window

The feature has to be reachable without editing `config.json`, so it lands in
the settings window's existing **Transcription** section (tk_ui.py), which
already carries Engine, Model and Language. Two new rows follow them:

- **Transcribe while I speak** — a checkbox bound to `stream_transcription`,
  in the same style as the GPU-graph checkbox. Explanation in the house voice:
  it starts decoding at the first pause instead of waiting for the whole
  recording, so a long dictation is nearly finished by the time you let go.
- **Segment length** — a slider over `segment_min_seconds`, and disabled while
  the checkbox is off, the way the GPU checkbox disables itself with no GPU.

The slider runs **30–90 s, not 0–90**. Below 30 seconds a segment is mostly
Whisper's own silence padding, which is where it hallucinates — a control that
lets you make the transcript worse is not a preference, it is a trap. The
window should not offer it.

`segment_max_seconds` and `segment_cut_silence` stay config-file-only, which
is the established pattern for `min_speech_seconds` and `chunk_size`. They are
safety fallbacks rather than preferences, and neither has a setting a user
could reason about from the window.

macOS is untouched: there is no settings window there, the menu bar holds the
options (appkit_ui.py). The config keys work on both platforms regardless.

## Configuration

New keys in `config.DEFAULTS`:

| key | default | meaning |
|---|---|---|
| `stream_transcription` | `true` | feature switch |
| `segment_min_seconds` | `30.0` | no cut before this much audio |
| `segment_max_seconds` | `120.0` | how long to wait for a usable pause |
| `segment_cut_silence` | `0.5` | pause length that qualifies as a cut point |

`stream_transcription: false` restores today's path exactly, and is the escape
hatch if anything about the feature turns out wrong in daily use.

## Testing

**Unit, no microphone and no GPU.**

- `SegmentCutter` over synthetic level sequences: never cuts before the
  minimum; cuts at the first qualifying pause after it; cuts at the quietest
  tracked candidate when the ceiling is reached; yields exactly one segment for
  a recording under the minimum; never closes a segment with no speech in it.
- `DictationSession`: out-of-order assembly, completion only after
  `set_final`, cancellation, empty parts.
- `JobQueue`: `active()` collapses a session to one row; `busy()` stays true
  while any partial is in flight.

**The measurement that proves the premise.** A real multi-minute Hungarian
dictation is decoded twice with the same `large-v3-turbo`: once whole, the way
it runs today, and once over the boundaries the cutter chooses. The two texts
are then compared word by word.

This is what settles "the text must not change" by evidence rather than by
argument. Whitespace and boundary punctuation differences are acceptable;
anything beyond that means the 30 s / 0.5 s defaults are too aggressive, and we
learn it before the feature ships. The probe script lives in `.tmp/` and runs
against a spooled WAV, so it needs no microphone.

**Manual end-to-end.** A real three-to-five-minute dictation: the text is
delivered once, history holds one entry, the spool is emptied, and the overlay
never grew a queue table.

## Rejected alternatives

**Overlapping "LocalAgreement" streaming** (the `whisper_streaming` approach):
re-decode a sliding buffer every second or two and emit only tokens that two
consecutive decodes agree on. It is the genuinely real-time option, but it
re-decodes the same audio repeatedly — the GPU runs continuously while you
speak — it needs word-level timestamps, and its confirmed-token policy can
produce a different final text than a full decode. That last point is
disqualifying: the text must not change. It also buys live typing, which is a
non-goal.

**A faster decoder** (faster-whisper / CTranslate2): four to ten times faster
on the same weights, with no change to the recording or delivery path. It does
not do what was asked — the wait still scales with clip length — but it
composes with this design and remains available later.

**The hosted realtime API** (`gpt-live-transcribe` over the Realtime
WebSocket): server-side VAD already segments on pauses and streams partial text
back, so it solves the same problem from the other side. Rejected for now on
three counts. It is a different model, so the "text must not change" guarantee
cannot be made — least of all for Hungarian, which the API docs do not list
explicitly. It costs $0.017/minute against a local decoder that costs nothing.
And it requires the network and sends every dictation off the machine, which
the current `stt_engine: "local"` setup deliberately avoids.

Worth noting: the session-with-ordered-partials layer this design introduces is
exactly what a realtime-API engine would also need. Choosing the local path now
does not close that door.
