# herdr-scribe — Design

**Status:** approved scope (Part 1). Build per `EXTRACTION-BRIEF.md`.

## What it is

A herdr plugin that turns a live meeting into text with no audio recording. Mic
(and optional call loopback) → streaming speech-to-text → a live transcript pane
+ a live analyst pane. On stop: write the transcript, destroy the in-memory
audio, and generate a plain meeting note.

## Scope

Included (generic, publishable):
- Live no-recording capture + streaming transcription (RAM-only).
- Live transcript pane.
- Live analyst pane (rolling brief via a headless model).
- Optional remote-participant loopback capture (`[me]`/`[them]` tags).
- Optional screen-OCR context.
- Glossary hotwords, consent flagging.
- Generic on-stop meeting-note generator (attendees / decisions / owner-attributed
  action items) — **no** domain-specific framing.

Explicitly out of scope (belongs in a private downstream layer, reached via the
`SCRIBE_ON_STOP` hook, never shipped here):
- Any privilege / confidentiality classification of the note.
- Any records-retention / legal-hold gating of destruction.
- Any routing of the note into domain-specific folders, matters, or vaults.
- Any downstream document generation (decks, memos, etc.).

## Plugin surface

- Actions: `start` (opts `--consent one-party|all-party`, `--topic`,
  `--attendees`, `--teams`, `--no-analyst`, `--analyst-interval N`, `--model ID`),
  `stop`, `status`, `abort`.
- Panes: a transcript pane and an analyst pane, opened on `start`.
- Config via env / `scribe.conf`: capture source, STT model, output dir, loopback
  exe path (host-specific), analyst model + interval, `SCRIBE_ON_STOP` hook.

## Architecture

1. **Capture.** Read the mic as raw PCM from the system capture source (on WSL2,
   WSLg/PulseAudio). Never write the audio to disk — pipe it straight to the
   transcriber. Optional second stream: a Windows userland loopback of the
   default render device (remote participants), converted and fed to a second
   transcriber; its lines are tagged `[them]`, mic lines `[me]`.
2. **Transcribe.** Stream PCM into a local STT engine (default `faster-whisper`,
   `base.en` — chosen because it runs faster than real time; larger models fall
   behind and drop live audio). Append recognized text to a transcript file in a
   RAM filesystem (`/dev/shm/scribe/<id>/`). No audio file is created at any point.
3. **Transcript pane.** `tail`-follow the transcript file in a herdr pane.
4. **Analyst pane.** Every N seconds, run a headless LLM command over the new
   transcript delta to produce a short rolling brief (Now / Commitments /
   Open-questions / Watch), shown in a second pane. Analysis is ephemeral and
   destroyed with the meeting.
5. **Stop.** Write the transcript to the output dir; **destroy** the RAM
   directory and all buffers; run the `SCRIBE_ON_STOP` hook against the written
   transcript (default: the generic note generator). `abort` discards everything
   with no note. `status` reports the running meeting.

## The `SCRIBE_ON_STOP` seam

`stop` invokes `SCRIBE_ON_STOP "<transcript-path>" "<meta...>"`. This is the one
extension point: the public default generates a generic note; a private consumer
can point it at its own pipeline. Keeping this a clean hook is what lets a
downstream/private layer sit on top **without forking the capture engine**.

## Platforms

Linux / WSL2 for capture + transcription. The optional loopback needs a Windows
host (userland only — no special privileges, no meeting-app or tenant access).
Everything host-specific is a configurable path with no default that assumes any
particular machine.
