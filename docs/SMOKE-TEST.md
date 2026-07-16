# herdr-scribe — Pre-Public Smoke Test

The unit suite runs without a mic, herdr, or a model (stubbed). Before this
plugin goes public, it must also pass a **live** smoke test on a real machine —
the integration paths (real STT streaming, mic capture, herdr panes, the Windows
loopback) cannot be verified in a headless/CI environment.

Run the preflight, then walk the manual checklist:

```
bash scripts/smoke.sh
```

## Gate 1 — Automated preflight (scripts/smoke.sh)

Passes when: required deps present (`python3`, `faster-whisper`, `parec`), the
unit suite is green, and the sanitization gate is HARD-clean. `ffmpeg`, `herdr`,
and the LLM command are reported as optional-missing where applicable.

## Gate 2 — Live functional (manual, real machine)

| # | Check | Pass criteria |
|---|-------|---------------|
| 1 | Real capture | Transcript pane fills as you speak; `/dev/shm/scribe/<id>/` holds **only** text files — **no audio file** |
| 2 | Analyst pane | A rolling Now/Commitments/Open-questions brief appears within ~1 interval |
| 3 | Stop | Transcript written to `$SCRIBE_OUTPUT_DIR`; RAM dir **gone**; a `<stem>.note.md` note produced |
| 4 | Abort | Meeting dir gone; **no** note written |
| 5 | Screen-OCR (opt) | With `SCRIBE_SCREEN_OCR_CMD` set, the brief reflects on-screen context |
| 6 | `--teams` loopback (Windows) | Remote audio appears as intelligible `[them]` lines (validates the ffmpeg resample — **format contract is unverified until this passes**) |

## Gate 3 — herdr integration + schema (manual)

- Install in a **real** herdr; confirm the manifest loads and the transcript +
  analyst panes open on `start` and close on `stop`/`abort`.
- `herdr-plugin.toml`'s `panes` / `actions.options` schema was **inferred** (no
  live herdr was available at build time — see the note atop that file). If
  herdr rejects it, adjust to herdr's actual schema. The four `actions` alone
  are the minimum that must work.

## Gate 4 — Human review

Read the full diff. Confirm no personal/organization identifier slipped in
(`bash scripts/sanitization-gate.sh` is necessary but not sufficient — eyeball
README/examples/comments too).

## Flip to public

Only when Gates 1–4 pass:

1. Make the repo public.
2. Add the GitHub topic **`herdr-plugin`** — the [herdr marketplace](https://herdr.dev/plugins)
   discovers listings automatically from that topic. No PR or review needed.
3. (Optional) bump `version` in `herdr-plugin.toml` to `1.0.0`.

> Known live-only risk to watch: Gate 2/#6 — the WASAPI loopback input format is
> a common-case assumption (32-bit float / 48 kHz / stereo), now resampled via
> ffmpeg and overridable by env. Confirm real remote audio is intelligible
> before advertising `--teams` as working.
