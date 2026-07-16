# herdr-scribe Implementation Plan

> **For agentic workers:** execute task-by-task (TDD). Steps use `- [ ]`.

**Goal:** Build the no-recording meeting-transcription herdr plugin described in `DESIGN.md`/`EXTRACTION-BRIEF.md`.

**Architecture:** A bash orchestrator (`scribe.sh`) manages a meeting lifecycle backed by a RAM-only directory (no audio file ever). A Python worker (`scribe-transcribe.py`) streams raw PCM through a pluggable STT backend into a transcript file; a `fake` backend makes it testable with no model/mic. `scribe-analyst.sh` loops a headless LLM over the transcript delta into a second pane. On stop, the transcript is written out, buffers destroyed, and a pluggable `SCRIBE_ON_STOP` hook runs (default `scribe-notes`, a generic note). Pure helpers are exercised via hidden `--flags` (no mic/herdr/model needed); integration paths use env-overridable command seams.

**Tech stack:** Bash (`set -euo pipefail`), Python 3, `faster-whisper` (default STT, stubbed in tests), a configurable headless LLM CLI (`SCRIBE_LLM_CMD`, default `claude -p`), herdr ≥ 0.7.0, pytest.

## Global Constraints

- **No recording:** audio only in an in-memory pipe; never open an audio file for writing; transcript in `${SCRIBE_RAMROOT:-/dev/shm}/scribe/<id>/`; destroyed on stop/abort. A test asserts no non-text file is created and the RAM dir is gone after stop.
- **`SCRIBE_ON_STOP` seam:** `stop` writes the transcript, destroys buffers, then runs `${SCRIBE_ON_STOP:-scribe-notes} <transcript-path>`. Engine stays unaware of downstream.
- **Everything host-specific is env-configurable** with a neutral default or none: `SCRIBE_RAMROOT`, `SCRIBE_OUTPUT_DIR`, `SCRIBE_STT_BACKEND`, `SCRIBE_LLM_CMD`, `SCRIBE_LOOPBACK_EXE`, `SCRIBE_CONF`. No absolute path may name a real user/machine/org.
- **Model-agnostic:** analyst + notes call `SCRIBE_LLM_CMD`.
- **Fail-safe on stop:** if the hook/LLM fails, still write the transcript and report; never lose the meeting.
- **Sanitization:** `scripts/sanitization-gate.sh` HARD list must be clean before any public push. Neutral commit identity. No denylist identifier anywhere.
- **Test seams:** `SCRIBE_STT_BACKEND=fake` (worker treats each stdin line as one recognized utterance), `SCRIBE_LLM_CMD` set to a stub script, `SCRIBE_RAMROOT`/`SCRIBE_OUTPUT_DIR` to tmp dirs. Tests must pass with no mic, herdr, model, or network.

## File Structure

- `scribe.sh` — orchestrator + hidden test flags (create/modify)
- `scribe-transcribe.py` — streaming STT worker with pluggable backend
- `scribe-analyst.sh` — analyst delta loop
- `scribe-notes` — generic on-stop note generator
- `scribe-loopback.cs` + `scribe-loopback-setup.sh`, `scribe-screen-setup.sh` — optional host bridges (gated)
- `herdr-plugin.toml` — finalize (panes + start flags)
- `scribe.conf.example`, `glossary.txt.example`
- `tests/test_scribe.py` — pytest suite (subprocess against the scripts via hidden flags + stub backends)

---

### Task 1: scribe.sh core helpers + hidden test flags

**Files:** Create `scribe.sh`; Create `tests/test_scribe.py`.

**Interfaces (produced):** `validate_consent <v>` (0 for `one-party`/`all-party`, else 1); `slugify <s>` (lowercase, non-alnum→`-`, squeeze/trim); `meeting_id <topic>` (prints `<UTC-YYYYMMDD-HHMMSS>-<slug>`); `ram_dir <id>`/`out_dir` (resolve from `SCRIBE_RAMROOT`/`SCRIBE_OUTPUT_DIR`). Hidden flags: `--validate-consent`, `--slugify`, `--ram-dir`, `--out-dir`.

- [ ] **Step 1: failing tests**
```python
import os, subprocess, pathlib
SCRIPT = str(pathlib.Path(__file__).resolve().parents[1] / "scribe.sh")
def run(args, env=None, stdin=""):
    return subprocess.run(["bash", SCRIPT, *args], input=stdin,
                          capture_output=True, text=True, env={**os.environ, **(env or {})})
def test_consent_ok():        assert run(["--validate-consent","one-party"]).returncode==0
def test_consent_all_ok():    assert run(["--validate-consent","all-party"]).returncode==0
def test_consent_bad():       assert run(["--validate-consent","none"]).returncode!=0
def test_slugify():           assert run(["--slugify","Weekly Sync #2"]).stdout.strip()=="weekly-sync-2"
def test_ram_dir_env(tmp_path):
    out=run(["--ram-dir","abc"],env={"SCRIBE_RAMROOT":str(tmp_path)}).stdout.strip()
    assert out==str(tmp_path)+"/scribe/abc"
```
- [ ] **Step 2: run → fail** (`pytest tests/test_scribe.py -k "consent or slugify or ram_dir" -v`) — no scribe.sh.
- [ ] **Step 3: implement**
```bash
#!/usr/bin/env bash
# scribe.sh — live no-recording meeting transcription for herdr.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[[ -f "${SCRIBE_CONF:-$HERE/scribe.conf}" ]] && source "${SCRIBE_CONF:-$HERE/scribe.conf}"
RAMROOT="${SCRIBE_RAMROOT:-/dev/shm}"
OUTDIR="${SCRIBE_OUTPUT_DIR:-$PWD/meetings}"

validate_consent(){ case "${1:-}" in one-party|all-party) return 0;; *) return 1;; esac; }
slugify(){ echo "${1:-}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//'; }
ram_dir(){ echo "$RAMROOT/scribe/${1:?}"; }
out_dir(){ echo "$OUTDIR"; }

case "${1:-}" in
  --validate-consent) validate_consent "${2:-}"; exit $?;;
  --slugify) slugify "${2:-}"; exit 0;;
  --ram-dir) ram_dir "${2:-}"; exit 0;;
  --out-dir) out_dir; exit 0;;
esac
```
- [ ] **Step 4: run → pass.**
- [ ] **Step 5: commit** (`git add scribe.sh tests/test_scribe.py && git commit -m "feat: scribe.sh core helpers + test harness"`).

---

### Task 2: Meeting lifecycle (start/status/abort/stop) — no-recording guarantee

**Files:** Modify `scribe.sh`; Modify `tests/test_scribe.py`.

**Interfaces:** `start` (opts parsed: `--consent` (required), `--topic`, `--attendees`, `--teams`, `--no-analyst`, `--analyst-interval`, `--model`) → creates `$(ram_dir id)/` with `transcript.md`, `meta` (consent/topic/attendees/started), prints the id; `status` → prints running id or "none"; `abort <id>` → destroy ram dir, no note; `stop <id>` → copy transcript to `$(out_dir)/<id>.md`, destroy ram dir, run `${SCRIBE_ON_STOP:-$HERE/scribe-notes} <out-path>`. A single-meeting model (current id in `$RAMROOT/scribe/.current`).

- [ ] **Step 1: failing tests** — assert: `start` creates ram dir + `meta` with consent, prints id, writes NO file other than `*.md`/`meta`/pid text (loop the dir, fail on any binary/audio ext); `status` reports it; `stop` copies transcript to out dir THEN removes ram dir (assert ram gone, out file present); `SCRIBE_ON_STOP` (stubbed to `echo`+touch marker) received the out path; `abort` removes ram dir and writes no out file. (Use `SCRIBE_RAMROOT`/`SCRIBE_OUTPUT_DIR` tmp dirs; stub capture/transcribe so `start` doesn't spawn a real mic — gate the capture spawn behind `SCRIBE_CAPTURE_CMD`, default real, tests set it to `true`.)
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** the four subcommands + option parsing; capture spawned via `${SCRIBE_CAPTURE_CMD}` (composed in Task 4; here default to a no-op placeholder that tests override). Destroy = `rm -rf "$(ram_dir id)"`. Fail-safe: wrap `SCRIBE_ON_STOP` in `|| true` after the transcript is safely written; report hook failure.
- [ ] **Step 4: run → pass.**
- [ ] **Step 5: commit** (`feat: meeting lifecycle + no-recording/destroy-on-stop`).

---

### Task 3: scribe-transcribe.py streaming worker (pluggable STT backend)

**Files:** Create `scribe-transcribe.py`; Modify `tests/test_scribe.py`.

**Interfaces:** CLI `scribe-transcribe.py --transcript <path> [--channel me|them] [--glossary <path>]`. Reads stdin. Backend from `SCRIBE_STT_BACKEND` (`faster-whisper` default; `fake` = read stdin as UTF-8 lines, each line one utterance). Appends `"[<channel>] <text>"` lines to `--transcript`. Loads glossary (one hotword/line, `#` comments) and passes as `initial_prompt`/`hotwords` to the real backend; the fake backend just records that it was loaded. **Never opens an audio file.**

- [ ] **Step 1: failing tests** (fake backend):
```python
def test_transcribe_fake_tags(tmp_path):
    t=tmp_path/"tx.md"
    subprocess.run(["python3", str(pathlib.Path(SCRIPT).parent/"scribe-transcribe.py"),
        "--transcript",str(t),"--channel","them"],
        input="hello world\nsecond line\n", text=True,
        env={**os.environ,"SCRIBE_STT_BACKEND":"fake"}, check=True)
    lines=t.read_text().splitlines()
    assert lines==["[them] hello world","[them] second line"]
def test_transcribe_glossary_loaded(tmp_path):
    g=tmp_path/"g.txt"; g.write_text("# c\nAcme\nZephyr\n")
    t=tmp_path/"tx.md"
    r=subprocess.run(["python3",str(pathlib.Path(SCRIPT).parent/"scribe-transcribe.py"),
        "--transcript",str(t),"--glossary",str(g)],input="x\n",text=True,
        env={**os.environ,"SCRIBE_STT_BACKEND":"fake","SCRIBE_DEBUG":"1"},
        capture_output=True, check=True)
    assert "glossary:2" in r.stderr   # fake backend reports hotword count
```
- [ ] **Step 2: run → fail.**
- [ ] **Step 3: implement** with a `Backend` seam: `FakeBackend` (line-per-utterance) and `FasterWhisperBackend` (lazy-imports `faster_whisper`, streams PCM chunks). Only the fake path is unit-tested here; the real path is exercised on a live run. Append-and-flush per utterance. No `open(..., 'wb')` for audio anywhere.
- [ ] **Step 4: run → pass.**
- [ ] **Step 5: commit** (`feat: streaming STT worker with pluggable/fake backend`).

---

### Task 4: capture-pipeline composer (no file write) + mic/loopback wiring

**Files:** Modify `scribe.sh`; Modify `tests/test_scribe.py`.

**Interfaces:** `compose_capture <id>` prints the shell pipeline that reads the mic capture source and pipes raw PCM into `scribe-transcribe.py --transcript <ram>/transcript.md --channel me`; when `--teams` and `SCRIBE_LOOPBACK_EXE` exists, also composes a `[them]` second stream. Hidden flag `--compose-capture`.

- [ ] **Step 1: failing tests** — `--compose-capture id` output: contains `scribe-transcribe.py`, `--channel me`, the transcript path; contains **no** `>` redirect to an audio file and no filename ending in an audio extension; with `SCRIBE_TEAMS=1` + a fake existing `SCRIBE_LOOPBACK_EXE`, also contains `--channel them`; with teams but missing exe, prints a `warning:` to stderr and mic-only.
- [ ] **Step 2 → 5:** implement, test, commit (`feat: capture composer (mic + optional loopback), never writes audio`). Wire `start` to run `compose_capture` unless `SCRIBE_CAPTURE_CMD` overrides (tests).

---

### Task 5: scribe-analyst.sh (delta loop, stubbed LLM)

**Files:** Create `scribe-analyst.sh`; Modify `tests/test_scribe.py`.

**Interfaces:** `scribe-analyst.sh <transcript> <analyst-out> [--interval N]`. Every interval: read new lines since last byte offset; if any, run `${SCRIBE_LLM_CMD}` with a rolling-brief prompt over the delta; write result to `<analyst-out>`. Exits when transcript's meeting dir disappears (stop). Hidden flag `--analyst-once <transcript> <out>` runs a single tick for tests. `--no-analyst` (in scribe.sh) skips spawning it.

- [ ] **Step 1: failing tests** — set `SCRIBE_LLM_CMD` to a stub (`printf 'BRIEF: %s'` style script that echoes its prompt); `--analyst-once` over a 2-line transcript writes a brief containing evidence of those lines; a second `--analyst-once` with no new lines does not rewrite (offset respected). Delta offset stored in `<analyst-out>.offset`.
- [ ] **Step 2 → 5:** implement, test, commit (`feat: analyst delta loop`).

---

### Task 6: scribe-notes generic on-stop note (stubbed LLM) + hook default

**Files:** Create `scribe-notes`; Modify `scribe.sh` (default `SCRIBE_ON_STOP`); Modify `tests/test_scribe.py`.

**Interfaces:** `scribe-notes <transcript-path>` composes a **generic** prompt — "produce a meeting note: Attendees, Decisions, and an owner-attributed Action Items table" — runs `${SCRIBE_LLM_CMD}`, writes `<transcript-dir>/<stem>.note.md`. **No** privilege/confidentiality/retention/matter/legal wording. Fail-safe: LLM failure → leave the transcript, write a `.note.error` marker, exit nonzero without deleting anything.

- [ ] **Step 1: failing tests** — `--print-prompt <transcript>` (hidden) emits a prompt containing "Attendees"/"Decisions"/"Action" and NONE of: privilege, confidential, retention, matter, litigation, vault (assert absence); with a stub `SCRIBE_LLM_CMD`, `scribe-notes tx.md` writes `tx.note.md`; with a failing stub, transcript still exists and exit≠0.
- [ ] **Step 2 → 5:** implement, test, commit (`feat: generic on-stop note generator + hook default`).

---

### Task 7: finalize manifest (panes + flags) + optional bridges (gated)

**Files:** Modify `herdr-plugin.toml`; Create `scribe-loopback.cs`, `scribe-loopback-setup.sh`, `scribe-screen-setup.sh`; Modify `tests/test_scribe.py`.

**Interfaces:** manifest gains pane defs (transcript pane tail-following the transcript; analyst pane) and documents the `start` flags. Bridges are optional: `scribe-loopback-setup.sh` builds the C# loopback exe with in-box `csc.exe`; if csc/exe absent, scribe warns and runs mic-only. `scribe-screen-setup.sh` sets up OCR; absent → analyst runs without screen context.

- [ ] **Step 1: failing tests** — `tomllib.load` parses `herdr-plugin.toml`; asserts 4 actions (`start/stop/status/abort`) and 2 panes; a `--doctor` scribe.sh flag reports which optional bridges are available and never errors when they're missing.
- [ ] **Step 2 → 5:** implement, test, commit (`feat: manifest panes/flags + optional loopback/screen bridges (graceful)`). The C# source may be a minimal NAudio WASAPI-loopback capture writing raw PCM to stdout; it is **not** built/verified in CI (needs Windows) — mark it clearly as live-only.

---

### Task 8: templates + README + sanitization gate + full verification

**Files:** Create `scribe.conf.example`, `glossary.txt.example`; Modify `README.md`; run gate + suite.

- [ ] **Step 1:** write `scribe.conf.example` (every env var with a neutral default + comment) and `glossary.txt.example` (empty + instructions).
- [ ] **Step 2:** README usage section (install, key-bind, config, the `SCRIBE_ON_STOP` seam) with placeholder names only (Alice/Bob/"Acme standup").
- [ ] **Step 3:** `bash scripts/sanitization-gate.sh` → HARD clean (fix any hit); `python3 -m pytest tests/ -v` → all pass; `shellcheck scribe.sh scribe-analyst.sh scribe-notes scribe-loopback-setup.sh scribe-screen-setup.sh` → no new errors.
- [ ] **Step 4: commit** (`docs+chore: config/glossary templates, README, gate clean`).

---

## Verification boundary (state honestly in the deliverable)

Stub-verified here: all pure helpers, lifecycle + destroy-on-stop, fake-backend transcription, capture composer (no-audio-file assertion), analyst delta, notes prompt+dispatch, manifest validity, sanitization gate. **Needs a live run on a real machine** (NOT verifiable in this environment): real `faster-whisper` streaming quality/latency, PulseAudio/WSLg mic capture, herdr pane rendering, and the Windows WASAPI loopback exe. These are structured with stub seams but must be smoke-tested before public release.
