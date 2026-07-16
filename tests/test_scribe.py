import os
import re
import shutil
import subprocess
import pathlib
import tempfile
import time
import pytest


SCRIPT = str(pathlib.Path(__file__).resolve().parents[1] / "scribe.sh")


def run(args, env=None, stdin=""):
    """Run scribe.sh with args; return CompletedProcess."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", SCRIPT, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=merged_env,
    )


# Task 1: core helpers + hidden test flags

def test_consent_one_party_ok():
    """validate_consent one-party should return 0."""
    result = run(["--validate-consent", "one-party"])
    assert result.returncode == 0


def test_consent_all_party_ok():
    """validate_consent all-party should return 0."""
    result = run(["--validate-consent", "all-party"])
    assert result.returncode == 0


def test_consent_bad():
    """validate_consent with invalid value should return non-zero."""
    result = run(["--validate-consent", "none"])
    assert result.returncode != 0


def test_slugify():
    """slugify should lowercase, replace non-alnum with -, squeeze/trim."""
    result = run(["--slugify", "Weekly Sync #2"])
    assert result.stdout.strip() == "weekly-sync-2"


def test_slugify_multiple_spaces():
    """slugify should squeeze multiple spaces into single dash."""
    result = run(["--slugify", "Foo   Bar"])
    assert result.stdout.strip() == "foo-bar"


def test_slugify_leading_trailing():
    """slugify should trim leading/trailing dashes."""
    result = run(["--slugify", "---foo---"])
    assert result.stdout.strip() == "foo"


def test_ram_dir_env(tmp_path):
    """ram_dir should use SCRIBE_RAMROOT env var."""
    result = run(
        ["--ram-dir", "abc"],
        env={"SCRIBE_RAMROOT": str(tmp_path)},
    )
    expected = str(tmp_path) + "/scribe/abc"
    assert result.stdout.strip() == expected


def test_ram_dir_default():
    """ram_dir should default to /dev/shm if SCRIBE_RAMROOT unset."""
    result = run(
        ["--ram-dir", "test-id"],
        env={"SCRIBE_RAMROOT": ""},  # unset
    )
    # Default should be /dev/shm
    expected = "/dev/shm/scribe/test-id"
    assert result.stdout.strip() == expected


def test_out_dir_env(tmp_path):
    """out_dir should use SCRIBE_OUTPUT_DIR env var."""
    result = run(
        ["--out-dir"],
        env={"SCRIBE_OUTPUT_DIR": str(tmp_path)},
    )
    assert result.stdout.strip() == str(tmp_path)


def test_out_dir_default():
    """out_dir should default to ./meetings if SCRIBE_OUTPUT_DIR unset."""
    result = run(
        ["--out-dir"],
        env={"SCRIBE_OUTPUT_DIR": ""},  # unset
    )
    # Default should be $PWD/meetings
    assert "meetings" in result.stdout.strip()


# Task 2: meeting lifecycle (start/status/abort/stop) — no-recording guarantee

def scribe_env(tmp_path, **extra):
    """Isolated RAM/output dirs + stubbed capture command (no real mic)."""
    env = {
        "SCRIBE_RAMROOT": str(tmp_path / "ram"),
        "SCRIBE_OUTPUT_DIR": str(tmp_path / "out"),
        "SCRIBE_CAPTURE_CMD": "true",
    }
    env.update(extra)
    return env


def write_stub_hook(path, body):
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def test_start_requires_consent(tmp_path):
    """start with no --consent must fail."""
    result = run(["start"], env=scribe_env(tmp_path))
    assert result.returncode != 0


def test_start_rejects_bad_consent(tmp_path):
    """start --consent <invalid> must fail."""
    result = run(["start", "--consent", "nope"], env=scribe_env(tmp_path))
    assert result.returncode != 0


def test_start_creates_ram_dir_with_transcript_and_meta(tmp_path):
    """start creates <ram_dir>/transcript.md + meta and prints the id."""
    env = scribe_env(tmp_path)
    result = run(["start", "--consent", "one-party", "--topic", "Weekly Sync"], env=env)
    assert result.returncode == 0
    meeting_id = result.stdout.strip()
    assert meeting_id  # non-empty id printed
    ram_dir = tmp_path / "ram" / "scribe" / meeting_id
    assert (ram_dir / "transcript.md").is_file()
    assert (ram_dir / "meta").is_file()


def test_start_meta_contains_consent_topic_attendees_started(tmp_path):
    """meta records consent/topic/attendees/started."""
    env = scribe_env(tmp_path)
    result = run(
        ["start", "--consent", "all-party", "--topic", "Acme Standup", "--attendees", "Alice,Bob"],
        env=env,
    )
    meeting_id = result.stdout.strip()
    meta = (tmp_path / "ram" / "scribe" / meeting_id / "meta").read_text()
    assert "consent: all-party" in meta
    assert "topic: Acme Standup" in meta
    assert "attendees: Alice,Bob" in meta
    assert "started:" in meta


def test_start_blocks_second_meeting(tmp_path):
    """Single-meeting model: a second start while one is active must fail."""
    env = scribe_env(tmp_path)
    first = run(["start", "--consent", "one-party"], env=env)
    assert first.returncode == 0
    second = run(["start", "--consent", "one-party"], env=env)
    assert second.returncode != 0


def test_status_none_when_nothing_running(tmp_path):
    """status with no meeting running reports 'none'."""
    result = run(["status"], env=scribe_env(tmp_path))
    assert result.returncode == 0
    assert result.stdout.strip() == "none"


def test_status_reports_running_id(tmp_path):
    """status reports the id of the currently running meeting."""
    env = scribe_env(tmp_path)
    started = run(["start", "--consent", "one-party"], env=env)
    meeting_id = started.stdout.strip()
    result = run(["status"], env=env)
    assert result.stdout.strip() == meeting_id


def test_abort_removes_ram_dir_and_writes_no_output(tmp_path):
    """abort destroys the ram dir and writes no out-dir note."""
    env = scribe_env(tmp_path)
    started = run(["start", "--consent", "all-party", "--topic", "Acme Standup"], env=env)
    meeting_id = started.stdout.strip()
    ram_dir = tmp_path / "ram" / "scribe" / meeting_id
    assert ram_dir.is_dir()

    result = run(["abort", meeting_id], env=env)
    assert result.returncode == 0
    assert not ram_dir.exists()
    assert not (tmp_path / "out" / f"{meeting_id}.md").exists()
    # abort clears the single-meeting marker too
    status = run(["status"], env=env)
    assert status.stdout.strip() == "none"


def test_stop_copies_transcript_then_destroys_ram_dir(tmp_path):
    """stop copies transcript.md to <out_dir>/<id>.md THEN removes the ram dir."""
    env = scribe_env(tmp_path, SCRIBE_ON_STOP="true")
    started = run(["start", "--consent", "one-party", "--topic", "Weekly Sync"], env=env)
    meeting_id = started.stdout.strip()
    ram_dir = tmp_path / "ram" / "scribe" / meeting_id

    # simulate some transcribed content before stop
    (ram_dir / "transcript.md").write_text("[me] hello world\n")

    result = run(["stop", meeting_id], env=env)
    assert result.returncode == 0

    out_file = tmp_path / "out" / f"{meeting_id}.md"
    assert out_file.is_file()
    assert out_file.read_text() == "[me] hello world\n"
    assert not ram_dir.exists()


def test_stop_runs_on_stop_hook_with_out_path(tmp_path):
    """SCRIBE_ON_STOP is invoked with the written-out transcript path."""
    marker = tmp_path / "marker.txt"
    hook = tmp_path / "hook.sh"
    write_stub_hook(hook, f'echo "$1" > "{marker}"')
    env = scribe_env(tmp_path, SCRIBE_ON_STOP=str(hook))

    started = run(["start", "--consent", "one-party"], env=env)
    meeting_id = started.stdout.strip()
    result = run(["stop", meeting_id], env=env)
    assert result.returncode == 0

    out_file = tmp_path / "out" / f"{meeting_id}.md"
    assert marker.read_text().strip() == str(out_file)


def test_stop_hook_failure_is_non_fatal(tmp_path):
    """Fail-safe: a failing on-stop hook must not lose the transcript or fail stop."""
    hook = tmp_path / "failhook.sh"
    write_stub_hook(hook, "exit 1")
    env = scribe_env(tmp_path, SCRIBE_ON_STOP=str(hook))

    started = run(["start", "--consent", "one-party"], env=env)
    meeting_id = started.stdout.strip()
    result = run(["stop", meeting_id], env=env)

    assert result.returncode == 0  # stop itself still succeeds
    out_file = tmp_path / "out" / f"{meeting_id}.md"
    assert out_file.is_file()  # transcript preserved despite hook failure
    assert result.stderr.strip() != ""  # hook failure is reported


def test_stop_with_no_id_uses_current_meeting(tmp_path):
    """stop with no explicit id resolves the single running meeting."""
    env = scribe_env(tmp_path, SCRIBE_ON_STOP="true")
    started = run(["start", "--consent", "one-party"], env=env)
    meeting_id = started.stdout.strip()

    result = run(["stop"], env=env)
    assert result.returncode == 0
    assert (tmp_path / "out" / f"{meeting_id}.md").is_file()


def test_stop_no_meeting_running_errors(tmp_path):
    """stop with nothing running fails cleanly."""
    result = run(["stop"], env=scribe_env(tmp_path))
    assert result.returncode != 0


def test_abort_no_meeting_running_errors(tmp_path):
    """abort with nothing running fails cleanly."""
    result = run(["abort"], env=scribe_env(tmp_path))
    assert result.returncode != 0


def test_no_recording_ram_dir_contains_only_text_files(tmp_path):
    """Global Constraint: across start->stop, ram dir holds only text files
    (transcript.md/meta/*.pid), no audio file is ever created, and the ram
    dir is fully gone after stop while the out file exists."""
    env = scribe_env(tmp_path, SCRIBE_ON_STOP="true")
    started = run(
        ["start", "--consent", "one-party", "--topic", "Weekly Sync #2"],
        env=env,
    )
    assert started.returncode == 0
    meeting_id = started.stdout.strip()
    ram_dir = tmp_path / "ram" / "scribe" / meeting_id
    assert ram_dir.is_dir()

    allowed_names = {"transcript.md", "meta"}
    for f in ram_dir.rglob("*"):
        if f.is_file():
            assert f.name in allowed_names or f.suffix == ".pid", (
                f"unexpected non-text file in ram dir: {f}"
            )
            data = f.read_bytes()
            assert b"\x00" not in data  # no binary/audio content

    result = run(["stop", meeting_id], env=env)
    assert result.returncode == 0
    assert not ram_dir.exists()  # ram dir fully destroyed
    assert (tmp_path / "out" / f"{meeting_id}.md").is_file()  # out file exists


# Task 3: scribe-transcribe.py streaming worker (pluggable STT backend)

TRANSCRIBE_SCRIPT = str(
    pathlib.Path(__file__).resolve().parents[1] / "scribe-transcribe.py"
)


def run_transcribe(args, env=None, stdin=""):
    """Run scribe-transcribe.py with args; return CompletedProcess."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["python3", TRANSCRIBE_SCRIPT, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=merged_env,
    )


def test_transcribe_fake_tags(tmp_path):
    """fake backend: each stdin line is one utterance, tagged with --channel."""
    t = tmp_path / "tx.md"
    result = run_transcribe(
        ["--transcript", str(t), "--channel", "them"],
        env={"SCRIBE_STT_BACKEND": "fake"},
        stdin="hello world\nsecond line\n",
    )
    assert result.returncode == 0
    lines = t.read_text().splitlines()
    assert lines == ["[them] hello world", "[them] second line"]


def test_transcribe_glossary_loaded(tmp_path):
    """glossary file (# comments/blanks ignored) is loaded; fake backend
    reports the hotword count on stderr when SCRIBE_DEBUG=1."""
    g = tmp_path / "g.txt"
    g.write_text("# c\nAcme\nZephyr\n")
    t = tmp_path / "tx.md"
    result = run_transcribe(
        ["--transcript", str(t), "--glossary", str(g)],
        env={"SCRIBE_STT_BACKEND": "fake", "SCRIBE_DEBUG": "1"},
        stdin="x\n",
    )
    assert result.returncode == 0
    assert "glossary:2" in result.stderr


def test_transcribe_default_channel_is_me(tmp_path):
    """When --channel is omitted, utterances are tagged [me]."""
    t = tmp_path / "tx.md"
    result = run_transcribe(
        ["--transcript", str(t)],
        env={"SCRIBE_STT_BACKEND": "fake"},
        stdin="hi there\n",
    )
    assert result.returncode == 0
    assert t.read_text().splitlines() == ["[me] hi there"]


def test_transcribe_empty_stdin_empty_transcript(tmp_path):
    """Empty stdin produces an empty (but existing) transcript, exit 0."""
    t = tmp_path / "tx.md"
    result = run_transcribe(
        ["--transcript", str(t)],
        env={"SCRIBE_STT_BACKEND": "fake"},
        stdin="",
    )
    assert result.returncode == 0
    assert t.read_text() == ""


def test_transcribe_source_never_writes_audio_file():
    """Global Constraint: never open an audio file for writing anywhere in
    the worker source — no binary-write file opens ('wb'/'ab'), and no
    audio-extension literal is ever paired with a write mode."""
    src = pathlib.Path(TRANSCRIBE_SCRIPT).read_text()
    assert "'wb'" not in src and '"wb"' not in src
    assert "'ab'" not in src and '"ab"' not in src
    for ext in (".wav", ".mp3", ".pcm", ".flac", ".m4a", ".ogg"):
        assert ext not in src, f"audio extension literal {ext!r} found in worker source"


# Task 4: capture-pipeline composer (no file write) + mic/loopback wiring

AUDIO_EXTENSIONS = (".wav", ".mp3", ".pcm", ".flac", ".m4a", ".ogg")


def compose_env(tmp_path, **extra):
    """Isolated RAM/output dirs for --compose-capture (no real mic/exe needed)."""
    env = {
        "SCRIBE_RAMROOT": str(tmp_path / "ram"),
        "SCRIBE_OUTPUT_DIR": str(tmp_path / "out"),
    }
    env.update(extra)
    return env


def test_compose_capture_mic_pipeline_targets_transcribe_worker(tmp_path):
    """--compose-capture <id> prints a pipeline feeding raw mic capture into
    scribe-transcribe.py --channel me with the right transcript path."""
    env = compose_env(tmp_path)
    result = run(["--compose-capture", "meeting-abc"], env=env)
    assert result.returncode == 0
    out = result.stdout
    assert "scribe-transcribe.py" in out
    assert "--channel me" in out
    transcript_path = str(tmp_path / "ram" / "scribe" / "meeting-abc" / "transcript.md")
    assert transcript_path in out


def test_compose_capture_never_writes_an_audio_file(tmp_path):
    """Global Constraint: the composed pipeline pipes raw PCM straight into
    the transcriber — no intermediate audio file, no redirect to one. (A
    stderr-to-/dev/null redirect like `2>/dev/null` in cleanup plumbing is
    fine; what's banned is any `>`/`>>` aimed at an audio-extension file.)"""
    env = compose_env(tmp_path)
    result = run(["--compose-capture", "meeting-abc"], env=env)
    out = result.stdout
    for ext in AUDIO_EXTENSIONS:
        assert ext not in out, f"audio extension literal {ext!r} found in composed pipeline"
    assert not re.search(r">>?\s*\S*\.(wav|mp3|pcm|flac|m4a|ogg)\b", out), (
        "composed pipeline must never redirect to an audio file"
    )


def test_compose_capture_mic_only_when_teams_not_requested(tmp_path):
    """Without --teams/SCRIBE_TEAMS, no [them] stream is composed."""
    env = compose_env(tmp_path)
    result = run(["--compose-capture", "meeting-abc"], env=env)
    assert "--channel them" not in result.stdout


def test_compose_capture_teams_adds_them_stream_when_loopback_exe_exists(tmp_path):
    """SCRIBE_TEAMS=1 + an existing SCRIBE_LOOPBACK_EXE composes a second
    [them] stream from the loopback exe into its own transcriber."""
    exe = tmp_path / "fake-loopback-exe"
    exe.write_text("#!/usr/bin/env bash\necho fake-loopback\n")
    exe.chmod(0o755)
    env = compose_env(tmp_path, SCRIBE_TEAMS="1", SCRIBE_LOOPBACK_EXE=str(exe))
    result = run(["--compose-capture", "meeting-abc"], env=env)
    assert result.returncode == 0
    assert "--channel them" in result.stdout
    assert str(exe) in result.stdout


def test_compose_capture_teams_missing_exe_warns_and_falls_back_to_mic_only(tmp_path):
    """Teams requested but SCRIBE_LOOPBACK_EXE missing: warn on stderr, no
    [them] stream, mic-only output."""
    missing_exe = tmp_path / "no-such-loopback-exe"
    env = compose_env(tmp_path, SCRIBE_TEAMS="1", SCRIBE_LOOPBACK_EXE=str(missing_exe))
    result = run(["--compose-capture", "meeting-abc"], env=env)
    assert result.returncode == 0
    assert "warning" in result.stderr.lower()
    assert "--channel them" not in result.stdout
    assert "--channel me" in result.stdout


def test_compose_capture_source_is_env_configurable(tmp_path):
    """SCRIBE_CAPTURE_SOURCE overrides the mic capture source; the composed
    pipeline reflects it rather than a hardcoded device name."""
    env = compose_env(tmp_path, SCRIBE_CAPTURE_SOURCE="some.custom.source")
    result = run(["--compose-capture", "meeting-abc"], env=env)
    assert "some.custom.source" in result.stdout


def test_start_wires_compose_capture_by_default(tmp_path):
    """start with no SCRIBE_CAPTURE_CMD override runs the composed capture
    pipeline (not an inert no-op) as the meeting's capture process."""
    env = {
        "SCRIBE_RAMROOT": str(tmp_path / "ram"),
        "SCRIBE_OUTPUT_DIR": str(tmp_path / "out"),
        # Deliberately no SCRIBE_CAPTURE_CMD: exercise the real wiring.
        # No real mic/parec is required for this to spawn+exit quickly since
        # a nonexistent capture tool just fails inside the backgrounded
        # subshell without affecting start()'s own exit code.
    }
    result = run(["start", "--consent", "one-party", "--topic", "Weekly Sync"], env=env)
    assert result.returncode == 0
    meeting_id = result.stdout.strip()
    ram_dir = tmp_path / "ram" / "scribe" / meeting_id
    assert (ram_dir / "transcript.md").is_file()
    assert (ram_dir / "capture.pid").is_file()


# Task 5: scribe-analyst.sh (delta loop, stubbed LLM)

ANALYST_SCRIPT = str(
    pathlib.Path(__file__).resolve().parents[1] / "scribe-analyst.sh"
)


def run_analyst(args, env=None, stdin=""):
    """Run scribe-analyst.sh with args; return CompletedProcess."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", ANALYST_SCRIPT, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=merged_env,
    )


def test_analyst_once_writes_brief_and_offset(tmp_path):
    """--analyst-once over a 2-line transcript writes a brief reflecting
    those lines and creates <out>.offset."""
    transcript = tmp_path / "transcript.md"
    transcript.write_text("[me] hello world\n[them] second line\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, 'input="$(cat)"; printf "BRIEF: %s" "$input"')

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={"SCRIBE_LLM_CMD": str(llm)},
    )
    assert result.returncode == 0
    assert out.is_file()
    brief = out.read_text()
    assert "hello world" in brief
    assert "second line" in brief

    offset_file = tmp_path / "brief.md.offset"
    assert offset_file.is_file()
    assert offset_file.read_text().strip() == str(len(transcript.read_bytes()))


def test_analyst_once_no_new_lines_does_not_rewrite(tmp_path):
    """A second --analyst-once with no new transcript content since the
    last tick must not re-invoke the LLM or rewrite the brief."""
    transcript = tmp_path / "transcript.md"
    transcript.write_text("[me] hello world\n")
    out = tmp_path / "brief.md"
    calls = tmp_path / "calls.log"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(
        llm, f'echo call >> "{calls}"; input="$(cat)"; printf "BRIEF: %s" "$input"'
    )
    env = {"SCRIBE_LLM_CMD": str(llm)}

    first = run_analyst(["--analyst-once", str(transcript), str(out)], env=env)
    assert first.returncode == 0
    first_brief = out.read_text()
    assert calls.read_text().count("call") == 1

    second = run_analyst(["--analyst-once", str(transcript), str(out)], env=env)
    assert second.returncode == 0
    assert out.read_text() == first_brief  # brief unchanged
    assert calls.read_text().count("call") == 1  # LLM not invoked again


def test_analyst_once_empty_transcript_skips_llm(tmp_path):
    """An empty transcript has no delta to analyze: the tick is a pure
    no-op — the LLM is never invoked and neither the brief nor the offset
    file is written (there's nothing new relative to the implicit 0
    starting offset, so nothing to record)."""
    transcript = tmp_path / "transcript.md"
    transcript.write_text("")
    out = tmp_path / "brief.md"
    calls = tmp_path / "calls.log"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, f'echo call >> "{calls}"; cat')

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={"SCRIBE_LLM_CMD": str(llm)},
    )
    assert result.returncode == 0
    assert not calls.exists()
    assert not out.exists()
    assert not (tmp_path / "brief.md.offset").exists()


def test_analyst_once_delta_excludes_already_seen_lines(tmp_path):
    """Each tick only sends the transcript content appended since the
    previous tick's offset -- not the whole transcript again."""
    transcript = tmp_path / "transcript.md"
    transcript.write_text("[me] first line\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, 'input="$(cat)"; printf "BRIEF: %s" "$input"')
    env = {"SCRIBE_LLM_CMD": str(llm)}

    run_analyst(["--analyst-once", str(transcript), str(out)], env=env)
    assert "first line" in out.read_text()

    with transcript.open("a") as f:
        f.write("[them] second line\n")

    run_analyst(["--analyst-once", str(transcript), str(out)], env=env)
    second_brief = out.read_text()
    assert "second line" in second_brief
    assert "first line" not in second_brief  # only the new delta is sent


def test_analyst_once_llm_failure_is_non_fatal(tmp_path):
    """A failing LLM command must not crash the tick; the offset still
    advances and the failure is reported, but no partial brief is written."""
    transcript = tmp_path / "transcript.md"
    transcript.write_text("[me] hello\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, "exit 1")

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={"SCRIBE_LLM_CMD": str(llm)},
    )
    assert result.returncode == 0  # the tick itself doesn't crash
    assert not out.exists()  # no brief written on failure
    assert (tmp_path / "brief.md.offset").is_file()  # offset still advances
    assert result.stderr.strip() != ""  # failure is reported


# Task 6: scribe-notes generic on-stop note generator (stubbed LLM)

NOTES_SCRIPT = str(
    pathlib.Path(__file__).resolve().parents[1] / "scribe-notes"
)

DENYLIST_WORDS = (
    "privilege",
    "confidential",
    "retention",
    "matter",
    "litigation",
    "vault",
    "hold",
)


def run_notes(args, env=None, stdin=""):
    """Run scribe-notes with args; return CompletedProcess."""
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", NOTES_SCRIPT, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=merged_env,
    )


def test_notes_print_prompt_has_required_sections(tmp_path):
    """--print-prompt emits a prompt naming Attendees/Decisions/Action Items,
    without invoking any LLM command."""
    t = tmp_path / "tx.md"
    t.write_text("[me] hello\n[them] hi there\n")
    result = run_notes(["--print-prompt", str(t)])
    assert result.returncode == 0
    assert "Attendees" in result.stdout
    assert "Decisions" in result.stdout
    assert "Action" in result.stdout


def test_notes_print_prompt_has_no_privilege_framing(tmp_path):
    """The generic prompt must carry NONE of the privilege/confidentiality/
    retention/matter/litigation/vault/hold framing (case-insensitive)."""
    t = tmp_path / "tx.md"
    t.write_text("[me] hello\n")
    result = run_notes(["--print-prompt", str(t)])
    lowered = result.stdout.lower()
    for word in DENYLIST_WORDS:
        assert word not in lowered, f"denylisted word {word!r} found in prompt"


def test_notes_writes_note_file_from_stub_llm(tmp_path):
    """scribe-notes runs SCRIBE_LLM_CMD over the transcript and writes
    <transcript-dir>/<stem>.note.md with the stub's output."""
    t = tmp_path / "tx.md"
    t.write_text("[me] hello world\n[them] second line\n")
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, 'input="$(cat)"; printf "NOTE: %s" "$input"')

    result = run_notes([str(t)], env={"SCRIBE_LLM_CMD": str(llm)})
    assert result.returncode == 0

    note = tmp_path / "tx.note.md"
    assert note.is_file()
    content = note.read_text()
    assert "NOTE:" in content
    assert "hello world" in content
    assert "second line" in content


def test_notes_llm_failure_preserves_transcript_and_writes_error_marker(tmp_path):
    """Fail-safe: a failing SCRIBE_LLM_CMD must not delete/alter the
    transcript; a .note.error marker is written and scribe-notes exits
    nonzero."""
    t = tmp_path / "tx.md"
    t.write_text("[me] hello\n")
    llm = tmp_path / "failhook.sh"
    write_stub_hook(llm, "exit 1")

    result = run_notes([str(t)], env={"SCRIBE_LLM_CMD": str(llm)})
    assert result.returncode != 0
    assert t.is_file()  # transcript untouched
    assert t.read_text() == "[me] hello\n"
    error_marker = tmp_path / "tx.note.error"
    assert error_marker.is_file()
    note = tmp_path / "tx.note.md"
    assert not note.exists()


def test_analyst_loop_exits_when_meeting_dir_disappears(tmp_path):
    """The main loop (no --analyst-once) must exit cleanly once the
    transcript's parent directory no longer exists (meeting stopped)."""
    meeting_dir = tmp_path / "ram" / "scribe" / "abc"
    meeting_dir.mkdir(parents=True)
    transcript = meeting_dir / "transcript.md"
    transcript.write_text("[me] hi\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, "cat")

    proc = subprocess.Popen(
        ["bash", ANALYST_SCRIPT, str(transcript), str(out), "--interval", "1"],
        env={**os.environ, "SCRIBE_LLM_CMD": str(llm)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)
    shutil.rmtree(meeting_dir)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("scribe-analyst.sh loop did not exit after meeting dir was removed")
    assert proc.returncode == 0
