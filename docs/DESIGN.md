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
  `--attendees`, `--teams`, `--no-analyst`), `stop`, `status`, `abort`. Analyst
  cadence and the LLM command/model used by the analyst + notes are env-only
  (`SCRIBE_ANALYST_INTERVAL`, `SCRIBE_LLM_CMD`) — both are consumed by
  processes (the analyst pane, the on-stop hook) that run separately from
  `start` and so can't receive a per-invocation flag from it.
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
5. **Stop.** A fixed sequence (see `docs/PARITY-PORT.md` §5): write the
   transcript to the output dir → generate the note (core step, default
   `scribe-notes`; `SCRIBE_NOTES_CMD` replaces it) → run the optional gate
   (`SCRIBE_GATE_CMD`; exit 75 or any failure/timeout = hold → the meeting
   dir is **quarantined**, never destroyed; every outcome writes one audit
   line) → on a clear verdict **destroy** the RAM directory and all
   buffers → optionally classify/draft artifacts (`SCRIBE_ARTIFACTS=1`) →
   run the `SCRIBE_ON_STOP` hook. `abort` discards everything with no note.
   `status` reports the running meeting.
6. **Two-tier analyst.** The rolling-brief loop may emit one anchored
   `RETRIEVE:` trigger per cycle (stripped from the pane); with a corpus
   configured (`SCRIBE_DEEP_CORPUS_ROOT`), a detached, single-in-flight,
   hard-timeboxed worker answers it with a verbatim, path-cited passage
   appended to the pane (bounded tail; light rewrites never clobber it).
7. **Auto-artifacts.** After a clear stop, a classifier over the note
   proposes follow-up documents; strong candidates within a cap are drafted
   (local files only, never transmitted), everything else is queued in a
   per-meeting sidecar and approved later via the zero-model
   `scribe.sh artifacts` surface / review pane.

## The extension seams

- **`SCRIBE_ON_STOP`** — `stop` invokes it with the written transcript path
  after a clear verdict. The classic extension point: a private consumer
  points it at its own pipeline. If it is set (and no `SCRIBE_NOTES_CMD`),
  core skips its own note generation — the pre-gate behavior, unchanged.
- **`SCRIBE_GATE_CMD`** — the post-note gate (§5 of the parity brief): core
  ships the mechanism (hold/quarantine/audit, fail closed both ways) and no
  policy; a downstream layer plugs its rules in here without core ever
  learning them.

Keeping these clean hooks is what lets a downstream/private layer sit on top
**without forking the capture engine**.

## Platforms

Linux / WSL2 for capture + transcription. The optional loopback needs a Windows
host (userland only — no special privileges, no meeting-app or tenant access).
Everything host-specific is a configurable path with no default that assumes any
particular machine.
