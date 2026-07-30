#!/usr/bin/env bash
# smoke.sh — pre-public go/no-go preflight for herdr-scribe.
#
# Runs the checks that CAN be automated (dependencies, unit suite, sanitization
# gate, --doctor), then prints the manual live checklist that a human must walk
# on a real machine (mic, herdr panes, faster-whisper, optional --teams loopback)
# before flipping the repo public. See docs/SMOKE-TEST.md for pass criteria.
#
# Exit 0 = automated preflight passed (manual checklist still required).
# Exit 1 = a hard dependency is missing or an automated check failed.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

pass=0 warn=0 fail=0
ok(){   printf '  \033[32mOK\033[0m   %s\n' "$1"; pass=$((pass+1)); }
opt(){  printf '  \033[33mOPT\033[0m  %s\n' "$1"; warn=$((warn+1)); }
bad(){  printf '  \033[31mMISS\033[0m %s\n' "$1"; fail=$((fail+1)); }

have(){ command -v "$1" >/dev/null 2>&1; }

echo "== Dependencies =="
if have python3; then ok "python3"; else bad "python3 (required)"; fi
if python3 -c "import faster_whisper" 2>/dev/null; then ok "faster-whisper (python module)"
  else bad "faster-whisper — 'pip install faster-whisper' (required for real STT)"; fi
if have parec; then ok "parec (PulseAudio capture)"
  else bad "parec — install pulseaudio-utils (required for mic capture)"; fi
if have ffmpeg; then ok "ffmpeg (loopback resample)"
  else opt "ffmpeg — only needed for --teams loopback; mic-only works without it"; fi
if have herdr; then ok "herdr"
  else opt "herdr not on PATH — needed to run as a plugin (panes); scripts still testable standalone"; fi
# LLM command (analyst + notes). Default 'claude -p'; honor an override.
read -ra llm_parts <<< "${SCRIBE_LLM_CMD:-claude -p}"
if have "${llm_parts[0]}"; then ok "LLM command: ${llm_parts[*]}"
  else opt "LLM command '${llm_parts[0]}' not found — set SCRIBE_LLM_CMD to your headless LLM CLI"; fi

echo ""
echo "== Automated checks =="
if python3 -m pytest tests/ -q >/tmp/scribe-smoke-pytest.log 2>&1; then
  # Guard the grep pipe: under pipefail a no-match grep fails the substitution.
  ok "unit suite ($(grep -oE '[0-9]+ passed' /tmp/scribe-smoke-pytest.log | tail -1 || true))"
else
  bad "unit suite FAILED — see /tmp/scribe-smoke-pytest.log"; fi
if bash scripts/sanitization-gate.sh >/tmp/scribe-smoke-gate.log 2>&1; then
  ok "sanitization gate (HARD clean)"
else
  bad "sanitization gate FAILED — see /tmp/scribe-smoke-gate.log"; fi
bash scribe.sh --doctor 2>/dev/null | sed 's/^/  · /' || true

echo ""
echo "== Preflight result =="
printf "  %d ok · %d optional-missing · %d hard-missing/failed\n" "$pass" "$warn" "$fail"
if [ "$fail" -ne 0 ]; then
  echo "  NOT READY: resolve the MISS/FAILED items above."
else
  echo "  Automated preflight PASSED. Now walk the manual live checklist below."
fi

cat <<'MANUAL'

== Manual live checklist (a human, on a real machine) ==
These cannot be automated here. Do each and confirm the expected result.

 [ ] 1. Real capture. Start a short meeting and speak:
        bash scribe.sh start --consent one-party --topic "Smoke test"
        - transcript pane populates with your words in near-real-time
        - `ls /dev/shm/scribe/<id>/` shows ONLY text (transcript.md/meta/*.pid) — no audio file
 [ ] 2. Analyst pane updates within ~1 interval with a Now/Commitments brief.
 [ ] 3. Stop and verify destruction + note:
        bash scribe.sh stop
        - transcript written to $SCRIBE_OUTPUT_DIR
        - /dev/shm/scribe/<id>/ is GONE (no residual audio anywhere)
        - a <stem>.note.md meeting note was produced
 [ ] 4. Abort path: start again, then `abort` — meeting dir gone, NO note written.
 [ ] 5. Screen-OCR (optional): with SCRIBE_SCREEN_OCR_CMD set, the analyst brief
        reflects on-screen context.
 [ ] 6. --teams loopback (Windows host only): build via scribe-loopback-setup.sh,
        set SCRIBE_LOOPBACK_EXE, start with --teams, confirm remote audio appears
        as [them] lines AND is intelligible (validates the ffmpeg resample —
        the format contract is unverified until this passes).
 [ ] 7. herdr integration: install as a plugin in a REAL herdr, confirm the
        manifest loads and the transcript + analyst panes open on start / close
        on stop. If herdr rejects the `panes`/`actions.options` schema, adjust
        herdr-plugin.toml to herdr's actual schema (it was inferred — see the
        note at the top of that file).
 [ ] 8. Human diff review of the whole repo.

Only when 1-8 pass: flip the repo public, then add the GitHub topic
`herdr-plugin` (the marketplace auto-discovers from that topic).
MANUAL
[ "$fail" -eq 0 ] || exit 1
