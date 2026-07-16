# herdr-scribe — Build & Extraction Brief

For the implementing agent (personal Claude Code). Build the plugin described in
`DESIGN.md` from scratch. A private, domain-specific ancestor of this tool
exists; you may consult it for technique, but **copy nothing verbatim** and
**never** carry over any identifier from the denylist in
`scripts/sanitization-gate.sh`. Build clean; the gate must pass before anything
goes public.

## Deliverables

1. `scribe.sh` — orchestrator: `start` / `stop` / `status` / `abort`.
2. `scribe-transcribe.py` — streaming STT worker (faster-whisper `base.en`),
   reads raw PCM on stdin, appends text to the RAM transcript, supports a
   `[me]`/`[them]` channel tag and a glossary/hotwords file.
3. `scribe-analyst.sh` — loops every `--analyst-interval` (default 60s), runs the
   headless LLM command over the transcript delta, writes the rolling brief.
4. `scribe-notes` — generic on-stop note generator: given a transcript, emit
   **attendees, decisions, and an owner-attributed action-item table**. No
   privilege, confidentiality, retention, matter, or domain framing of any kind.
5. Optional host bridges: `scribe-loopback.cs` + `scribe-loopback-setup.sh`
   (Windows userland WASAPI loopback via NAudio, built with in-box `csc.exe`;
   no elevated rights, no meeting-app/tenant access) and `scribe-screen-setup.sh`
   (screen OCR). Both optional; degrade to mic-only with a warning if absent.
6. `herdr-plugin.toml` — finish the starter manifest: add pane definitions for
   the transcript and analyst panes and the `start` option flags.
7. `scribe.conf.example`, `glossary.txt.example` — neutral templates.
8. `tests/` — cover the pure helpers (arg/consent/slug validation, note-format
   assembly, the on-stop hook dispatch, transcript-destroyed-on-stop). Tests must
   run without a mic, herdr, or a live model (stub the STT worker and the LLM
   command via env-overridable command paths).
9. `README.md` (present), `LICENSE` (MIT, present).

## Hard requirements

- **No recording.** Audio only ever exists in an in-memory pipe / ring buffer.
  Never open an audio file for writing. Transcript lives in `/dev/shm/scribe/<id>/`
  and is destroyed on `stop`/`abort`. Add a test asserting no audio file exists
  and the RAM dir is gone after stop.
- **`SCRIBE_ON_STOP` seam.** `stop` writes the transcript, destroys buffers, then
  runs `SCRIBE_ON_STOP "<transcript-path>"` (default = `scribe-notes`). This is
  the only extension point; keep the engine unaware of what runs downstream.
- **Everything host-specific is configurable** with a neutral default or no
  default — capture source, model, output dir, loopback exe path, LLM command.
  No absolute path may reference any real user, machine, or organization.
- **Model-agnostic.** The analyst and notes call a configurable command
  (`SCRIBE_LLM_CMD`, default `claude -p`). Works with any headless LLM CLI.
- **Fail-safe on stop.** If the note generator or hook fails, still write the
  transcript and report; never lose the meeting silently.

## Sanitization (non-negotiable before public)

1. Run `bash scripts/sanitization-gate.sh` — it greps the whole tree against a
   denylist of personal/organization identifiers. It **must exit 0 with zero
   hits**. Extend the denylist if you think of another identifier.
2. Commit authorship must be neutral (no real-name/employer email). Use a GitHub
   `noreply` identity.
3. README/tests/examples use placeholder names only (Alice, Bob, "Acme standup").
4. Keep the repo **private** until (1)–(3) pass AND a human has reviewed the full
   diff. Flip to public only then. Publishing to the marketplace = add the
   GitHub topic `herdr-plugin` after it's public.

## Suggested build order (TDD, small commits)

1. Manifest + `scribe.sh` skeleton (start/stop/status/abort, RAM dir lifecycle,
   destroy-on-stop test).
2. `scribe-transcribe.py` streaming worker + glossary support (stub-testable).
3. Transcript pane wiring.
4. `scribe-analyst.sh` + analyst pane (stub LLM).
5. `scribe-notes` generic generator + `SCRIBE_ON_STOP` dispatch.
6. Optional loopback + screen-OCR bridges (gated, degrade gracefully).
7. `scribe.conf.example`, glossary template, README polish.
8. Run the sanitization gate; human review; flip public; add `herdr-plugin` topic.

## Non-goals

Any privilege/confidentiality/retention/legal-hold/matter/vault/document-
generation behavior. Those live in a separate private layer that attaches via
`SCRIBE_ON_STOP` and are never part of this repo.
