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

This drops `start` / `stop` / `status` / `abort` actions and a transcript +
analyst pane pair into herdr. See `docs/DESIGN.md` for the full plugin
surface.

## Usage

### 1. Install

Install the plugin as above, or clone this repo and add it as a herdr plugin
directory directly — see `docs/DESIGN.md` for the manifest herdr reads
(`herdr-plugin.toml`).

### 2. Bind a key

Bind herdr keys (or menu entries) to the four actions in your herdr config,
e.g. (illustrative — match herdr's own binding syntax):

```
scribe.start  -> <leader>ss
scribe.stop   -> <leader>se
scribe.status -> <leader>s?
scribe.abort  -> <leader>sx
```

Once bound, `start` opens the transcript pane and (unless `--no-analyst`) the
analyst pane; both close automatically on `stop`/`abort`.

### 3. Configure

Copy the templates and fill in what applies to your machine — everything is
optional and has a neutral default:

```
cp scribe.conf.example scribe.conf
cp glossary.txt.example glossary.txt   # optional: hotword names/terms
```

`scribe.conf` is auto-sourced by `scribe.sh` on every run. Every variable it
sets is documented inline in `scribe.conf.example` — capture source, STT
backend, output directory, the LLM command used by the analyst/notes, the
optional Windows loopback and screen-OCR bridges, and more. Because
`scribe-analyst.sh` and the pane commands in `herdr-plugin.toml` are spawned
directly by herdr (not as children of `scribe.sh`), also `source
scribe.conf` from your shell profile (or wherever herdr launches its own
process from) if you want those to see the same config — see the note at
the top of `scribe.conf.example`.

Both `scribe.conf` and `glossary.txt` are gitignored — never commit your
real copies, only the `.example` templates.

### 4. Run a meeting

```
# Start a meeting. --consent is required: declare the recording-consent
# regime that applies to this call.
bash scribe.sh start --consent one-party --topic "Acme standup" --attendees "Alice, Bob"

# Also capture remote participants (needs the optional Windows loopback
# bridge — see "Optional bridges" below); falls back to mic-only with a
# warning if it isn't built.
bash scribe.sh start --consent one-party --teams

# Skip the live analyst pane for this meeting, or change its cadence:
bash scribe.sh start --consent one-party --no-analyst
bash scribe.sh start --consent one-party --analyst-interval 30

# Check what's running (prints the meeting id, or "none"):
bash scribe.sh status

# End the meeting: writes the transcript to SCRIBE_OUTPUT_DIR, destroys the
# RAM buffers, then runs the on-stop hook (see below) against the written
# transcript. Prints the transcript path.
bash scribe.sh stop

# Discard the meeting instead: destroys everything, writes nothing, no note.
bash scribe.sh abort
```

herdr's single-meeting model means only one meeting runs at a time; `start`
refuses to run again until the current one is `stop`ped or `abort`ed.

### 5. The `SCRIBE_ON_STOP` seam

On `stop`, once the transcript is safely written out and the RAM buffers are
destroyed, scribe runs:

```
${SCRIBE_ON_STOP:-scribe-notes} <transcript-path>
```

The default, `scribe-notes`, is a generic on-stop note generator: it feeds
the transcript to `SCRIBE_LLM_CMD` and writes a plain meeting note
(Attendees / Decisions / an owner-attributed Action Items table) alongside
the transcript — no privilege, confidentiality, retention, or domain-specific
framing of any kind.

This is the one extension point in the whole plugin: point `SCRIBE_ON_STOP`
at your own script to route the transcript into a different downstream
pipeline (a different note format, a different destination, additional
processing) without touching the capture engine at all. If the hook fails
for any reason, the transcript stays right where `stop` wrote it — a
downstream failure can never lose the meeting.

### 6. Optional bridges

Both are fully optional and degrade gracefully (mic-only / no screen
context) when unavailable — run `bash scribe.sh --doctor` any time to see
what's currently available on your machine:

```
bash scribe.sh --doctor
```

- `scribe-loopback-setup.sh` builds the Windows remote-participant loopback
  bridge (userland WASAPI via NAudio, compiled with the in-box `csc.exe`).
  Wire the result in via `SCRIBE_LOOPBACK_EXE`.
- `scribe-screen-setup.sh` wires up optional screen-OCR context for the
  analyst (needs a screenshot tool + `tesseract`). Wire the result in via
  `SCRIBE_SCREEN_OCR_CMD`.

## License

MIT — see `LICENSE`.
