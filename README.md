# herdr-scribe

Live, **no-recording** meeting transcription for [herdr](https://herdr.dev) — the terminal multiplexer for coding agents.

Your mic (and, optionally, remote participants on a video call) stream straight through a local speech-to-text model into a live transcript pane and a rolling "what's happening now" analyst pane. **No audio file is ever written** — audio lives only in an in-memory pipe and is destroyed when the meeting ends. On stop, scribe writes the transcript and generates a plain meeting note (attendees, decisions, action items).

> Status: in development. See `docs/EXTRACTION-BRIEF.md` for the build plan.

## Why

Meeting recorders create a file you then have to store, protect, and delete. scribe never creates one: it transcribes in real time from an in-memory audio pipe and keeps only text. You get a searchable transcript and a live brief without a recording sitting on disk.

## Features

- **No recording** — raw-PCM pipe → speech-to-text → RAM-only transcript (`/dev/shm`); no audio file, ever. Buffers destroyed on stop.
- **Live transcript pane** — streaming text as people talk.
- **Live analyst pane** — a headless model summarizes the transcript-so-far every N seconds into a rolling Now / Commitments / Open-questions brief.
- **Remote-participant capture (optional)** — on Windows/WSL, a userland loopback of the default render device captures the far end of a call; lines tag `[me]` / `[them]`.
- **Screen-OCR context (optional)** — periodically OCR the shared screen to feed the analyst.
- **Glossary hotwords** — bias the recognizer toward expected names/terms.
- **Consent-aware** — records the consent basis you declare (`one-party` / `all-party`) in the output.
- **Pluggable on-stop hook** — `SCRIBE_ON_STOP` runs any command against the finished transcript; the default is a generic meeting-note generator.

## Requirements

- herdr ≥ 0.7.0
- A speech-to-text engine (default: [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper), `base.en`)
- Linux or WSL2 with a working mic capture source (WSLg / PulseAudio)
- Optional: Windows host for remote-participant loopback capture
- A headless LLM command for the analyst / notes (configurable; default `claude -p`)

## Install

```
herdr plugin install <owner>/herdr-scribe
```

Then bind a key and set your config — see `docs/DESIGN.md`.

## License

MIT — see `LICENSE`.
