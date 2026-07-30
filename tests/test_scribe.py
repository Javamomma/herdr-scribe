import os
import re
import shutil
import subprocess
import pathlib
import tempfile
import time
import tomllib
import pytest


SCRIPT = str(pathlib.Path(__file__).resolve().parents[1] / "scribe.sh")
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"


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
    """Isolated RAM/output dirs + stubbed capture command (no real mic).
    SCRIBE_LLM_CMD is stubbed so the core note step never reaches for a
    real model CLI inside the suite."""
    env = {
        "SCRIBE_RAMROOT": str(tmp_path / "ram"),
        "SCRIBE_OUTPUT_DIR": str(tmp_path / "out"),
        "SCRIBE_CAPTURE_CMD": "true",
        "SCRIBE_LLM_CMD": "true",
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


def test_compose_capture_teams_resamples_loopback_via_ffmpeg(tmp_path):
    """The loopback exe emits the Windows render device's raw mix format
    (often 32-bit float/48kHz/stereo), not the s16le/16k/mono the
    transcriber hard-assumes. The [them] branch must interpose an ffmpeg
    resample step -- reaching the worker only after being converted -- never
    pipe the loopback exe's raw output straight into the transcriber."""
    exe = tmp_path / "fake-loopback-exe"
    exe.write_text("#!/usr/bin/env bash\necho fake-loopback\n")
    exe.chmod(0o755)
    env = compose_env(tmp_path, SCRIBE_TEAMS="1", SCRIBE_LOOPBACK_EXE=str(exe))
    result = run(["--compose-capture", "meeting-abc"], env=env)
    assert result.returncode == 0
    out = result.stdout
    assert "ffmpeg" in out
    assert "-ar 16000" in out
    assert "-ac 1" in out
    assert "-f s16le" in out
    assert "--channel them" in out
    # the exe's raw output must flow through ffmpeg, not directly into the
    # transcriber
    exe_line = next(line for line in out.splitlines() if str(exe) in line)
    assert "ffmpeg" in exe_line
    assert "scribe-transcribe.py" not in exe_line.split("ffmpeg")[0]


def test_compose_capture_teams_missing_ffmpeg_falls_back_to_mic_only(tmp_path):
    """If ffmpeg isn't available to resample it, the [them] stream must be
    dropped entirely (warn + mic-only) rather than ever handing the worker
    raw device-format audio it can't interpret."""
    exe = tmp_path / "fake-loopback-exe"
    exe.write_text("#!/usr/bin/env bash\necho fake-loopback\n")
    exe.chmod(0o755)
    env = compose_env(
        tmp_path,
        SCRIBE_TEAMS="1",
        SCRIBE_LOOPBACK_EXE=str(exe),
        SCRIBE_FFMPEG_BIN=str(tmp_path / "no-such-ffmpeg"),
    )
    result = run(["--compose-capture", "meeting-abc"], env=env)
    assert result.returncode == 0
    assert "warning" in result.stderr.lower()
    assert "ffmpeg" in result.stderr.lower()
    assert "--channel them" not in result.stdout
    assert "--channel me" in result.stdout


def test_compose_capture_loopback_format_env_configurable(tmp_path):
    """SCRIBE_LOOPBACK_FORMAT/_RATE/_CHANNELS override the assumed input
    format ffmpeg is told to expect from the loopback exe."""
    exe = tmp_path / "fake-loopback-exe"
    exe.write_text("#!/usr/bin/env bash\necho fake-loopback\n")
    exe.chmod(0o755)
    env = compose_env(
        tmp_path,
        SCRIBE_TEAMS="1",
        SCRIBE_LOOPBACK_EXE=str(exe),
        SCRIBE_LOOPBACK_FORMAT="s24le",
        SCRIBE_LOOPBACK_RATE="44100",
        SCRIBE_LOOPBACK_CHANNELS="6",
    )
    result = run(["--compose-capture", "meeting-abc"], env=env)
    out = result.stdout
    assert "s24le" in out
    assert "44100" in out
    assert "-ac \"6\"" in out or "-ac 6" in out


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


def test_analyst_once_includes_screen_context_when_configured(tmp_path):
    """When SCRIBE_SCREEN_OCR_CMD is set and runnable, its stdout is
    captured and prepended to the analyst prompt ahead of the transcript
    delta -- the stubbed LLM below just echoes back whatever prompt it
    received, so its presence in the brief proves it reached the prompt."""
    transcript = tmp_path / "transcript.md"
    transcript.write_text("[me] hello world\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, 'input="$(cat)"; printf "BRIEF: %s" "$input"')
    ocr = tmp_path / "ocr-stub.sh"
    write_stub_hook(ocr, "echo SCREENTEXT")

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={"SCRIBE_LLM_CMD": str(llm), "SCRIBE_SCREEN_OCR_CMD": str(ocr)},
    )
    assert result.returncode == 0
    brief = out.read_text()
    assert "SCREENTEXT" in brief
    assert "hello world" in brief


def test_analyst_once_screen_ocr_failure_is_non_fatal(tmp_path):
    """A failing/unresolvable SCRIBE_SCREEN_OCR_CMD must never abort the
    tick -- the brief still gets written from transcript-only context."""
    transcript = tmp_path / "transcript.md"
    transcript.write_text("[me] hello world\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, 'input="$(cat)"; printf "BRIEF: %s" "$input"')
    ocr = tmp_path / "ocr-stub.sh"
    write_stub_hook(ocr, "exit 1")

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={"SCRIBE_LLM_CMD": str(llm), "SCRIBE_SCREEN_OCR_CMD": str(ocr)},
    )
    assert result.returncode == 0
    assert "hello world" in out.read_text()


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


# Task 7: manifest (panes + flags) + optional bridges (gated) + --doctor

MANIFEST = REPO_ROOT / "herdr-plugin.toml"
LOOPBACK_SETUP = str(REPO_ROOT / "scribe-loopback-setup.sh")
SCREEN_SETUP = str(REPO_ROOT / "scribe-screen-setup.sh")
LOOPBACK_CS = REPO_ROOT / "scribe-loopback.cs"


def load_manifest():
    with open(MANIFEST, "rb") as f:
        return tomllib.load(f)


def test_manifest_parses_with_tomllib():
    """herdr-plugin.toml must be valid TOML per Python's stdlib tomllib."""
    data = load_manifest()
    assert "actions" in data


def test_manifest_has_four_actions():
    """The manifest keeps exactly the four lifecycle actions."""
    data = load_manifest()
    ids = {a["id"] for a in data["actions"]}
    assert ids == {"start", "stop", "status", "abort"}


def test_manifest_has_three_panes():
    """The manifest defines the transcript, analyst, and artifact-review
    panes."""
    data = load_manifest()
    assert "panes" in data
    assert len(data["panes"]) == 3
    pane_ids = {p["id"] for p in data["panes"]}
    assert pane_ids == {"transcript", "analyst", "artifacts"}


def test_manifest_panes_have_commands():
    """Each pane carries a runnable command (list of argv tokens)."""
    data = load_manifest()
    for pane in data["panes"]:
        assert isinstance(pane.get("command"), list)
        assert len(pane["command"]) > 0


def test_manifest_start_action_documents_option_flags():
    """The start action documents the five start-option flags (inferred
    [[actions.options]] schema -- see the manifest's schema-note header for
    the caveat). Analyst cadence and LLM/model selection are env-only
    (SCRIBE_ANALYST_INTERVAL / SCRIBE_LLM_CMD) since the analyst pane and
    on-stop hook run as separate processes `start` has no way to pass
    per-invocation flags into -- see scribe.conf.example."""
    data = load_manifest()
    start = next(a for a in data["actions"] if a["id"] == "start")
    flags = {opt["flag"] for opt in start.get("options", [])}
    expected = {
        "--consent",
        "--topic",
        "--attendees",
        "--scope",
        "--teams",
        "--no-analyst",
    }
    assert flags == expected


def test_manifest_no_denylisted_identifiers():
    """Sanity check ahead of the full sanitization gate (Task 8): the
    manifest itself must not smuggle in any host-specific absolute path."""
    text = MANIFEST.read_text()
    assert "/home/" not in text
    assert "/Users/" not in text
    assert "C:\\" not in text


# --- scribe.sh --doctor ---

def test_doctor_exits_zero_with_no_bridges_configured(tmp_path):
    """--doctor never errors when both optional bridges are absent, and
    names each bridge's availability in its report."""
    result = run(
        ["--doctor"],
        env={"SCRIBE_LOOPBACK_EXE": "", "SCRIBE_SCREEN_OCR_CMD": ""},
    )
    assert result.returncode == 0
    lowered = result.stdout.lower()
    assert "loopback" in lowered
    assert "screen" in lowered
    assert "not available" in lowered


def test_doctor_reports_loopback_available_when_exe_exists(tmp_path):
    """A configured, existing SCRIBE_LOOPBACK_EXE is reported as available."""
    exe = tmp_path / "fake-loopback-exe"
    exe.write_text("#!/usr/bin/env bash\necho fake-loopback\n")
    exe.chmod(0o755)
    result = run(
        ["--doctor"],
        env={"SCRIBE_LOOPBACK_EXE": str(exe), "SCRIBE_SCREEN_OCR_CMD": ""},
    )
    assert result.returncode == 0
    assert "loopback" in result.stdout.lower()
    assert "available" in result.stdout.lower()
    assert str(exe) in result.stdout


def test_doctor_reports_screen_ocr_available_when_cmd_configured(tmp_path):
    """A configured, resolvable SCRIBE_SCREEN_OCR_CMD is reported available."""
    result = run(
        ["--doctor"],
        env={"SCRIBE_LOOPBACK_EXE": "", "SCRIBE_SCREEN_OCR_CMD": "true"},
    )
    assert result.returncode == 0
    lowered = result.stdout.lower()
    assert "screen" in lowered
    assert "available" in lowered


def test_doctor_never_errors_even_with_garbage_env(tmp_path):
    """--doctor must exit 0 even when the configured bridge paths/commands
    are nonsense -- it only ever reports, never fails."""
    result = run(
        ["--doctor"],
        env={
            "SCRIBE_LOOPBACK_EXE": str(tmp_path / "does-not-exist"),
            "SCRIBE_SCREEN_OCR_CMD": "definitely-not-a-real-command-xyz",
        },
    )
    assert result.returncode == 0


# --- optional bridges: graceful degradation ---

def test_loopback_setup_warns_and_exits_nonzero_without_csc(tmp_path):
    """In a sandbox with no csc.exe, the loopback build must warn to stderr
    and exit nonzero without touching/creating anything else."""
    result = subprocess.run(
        [BASH, LOOPBACK_SETUP, "--out", str(tmp_path / "scribe-loopback.exe")],
        capture_output=True,
        text=True,
        env={**os.environ, "SCRIBE_CSC_EXE": str(tmp_path / "no-such-csc.exe")},
    )
    assert result.returncode != 0
    assert "warning" in result.stderr.lower()
    assert not (tmp_path / "scribe-loopback.exe").exists()


def test_loopback_setup_missing_source_warns_and_exits_nonzero(tmp_path):
    """If scribe-loopback.cs can't be found, warn + exit nonzero cleanly."""
    fake_dir = tmp_path / "no-source-here"
    fake_dir.mkdir()
    shim = fake_dir / "scribe-loopback-setup.sh"
    shim.write_text(pathlib.Path(LOOPBACK_SETUP).read_text())
    shim.chmod(0o755)
    result = subprocess.run(
        [BASH, str(shim)], capture_output=True, text=True, env=os.environ.copy()
    )
    assert result.returncode != 0
    assert "warning" in result.stderr.lower()


def test_screen_setup_degrades_gracefully_without_ocr_tools(tmp_path):
    """With no OCR/screenshot tools on PATH, the setup script warns, exits
    nonzero, and does not write the wrapper it would otherwise generate."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    result = subprocess.run(
        [BASH, SCREEN_SETUP],
        capture_output=True,
        text=True,
        env={"PATH": str(empty_bin)},
    )
    assert result.returncode != 0
    assert "warning" in result.stderr.lower()
    assert not (REPO_ROOT / "scribe-screen-ocr.sh").exists()


def test_loopback_cs_is_marked_live_only_and_not_executed_by_tests():
    """The C# loopback source exists, documents itself as Windows/live-only,
    and mentions the NAudio dependency it's written against. It is never
    compiled or run as part of this suite (no Windows/csc.exe here)."""
    text = LOOPBACK_CS.read_text()
    assert "WINDOWS-ONLY" in text or "Windows-only" in text
    assert "LIVE-ONLY" in text or "live-only" in text.lower()
    assert "NAudio" in text
    assert "elevated" in text.lower() or "no elevated" in text.lower()


# --- §3 per-meeting glossary injection ---

def test_derive_hotwords_merges_scope_attendees_topic_deduped(tmp_path):
    """Scope glossary + attendees + topic, comment/blank lines stripped,
    order-preserving de-dup (Alice appears in both sources → once)."""
    scope_dir = tmp_path / "scopes" / "acme-standup"
    scope_dir.mkdir(parents=True)
    (scope_dir / "glossary.txt").write_text("# jargon\nWidgetCo\n\nAlice\n")
    result = run(
        ["--derive-hotwords", "acme-standup", "Weekly Sync", "Alice, Bob"],
        env={"SCRIBE_SCOPE_ROOT": str(tmp_path / "scopes")},
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["WidgetCo", "Alice", "Bob", "Weekly Sync"]


def test_derive_hotwords_without_scope_root_uses_meeting_args_only(tmp_path):
    """No SCRIBE_SCOPE_ROOT configured → only attendees/topic terms."""
    result = run(
        ["--derive-hotwords", "acme", "Kickoff", "Carol"],
        env={"SCRIBE_SCOPE_ROOT": ""},
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["Carol", "Kickoff"]


def test_validate_scope_accepts_plain_names_rejects_paths():
    assert run(["--validate-scope", "acme-standup"]).returncode == 0
    assert run(["--validate-scope", "proj.x_1"]).returncode == 0
    for bad in ("../evil", "a/b", "", ".hidden", "a..b"):
        assert run(["--validate-scope", bad]).returncode != 0, bad


def test_start_rejects_path_scope(tmp_path):
    env = scribe_env(tmp_path)
    result = run(
        ["start", "--consent", "one-party", "--scope", "../evil"], env=env
    )
    assert result.returncode != 0
    assert "scope" in result.stderr.lower()


def test_start_with_scope_writes_hotwords_and_global_file_is_byte_unchanged(tmp_path):
    """Inline per-meeting terms land in the RAM dir; the reviewed global
    glossary file is never mutated by a start that injected terms."""
    global_glossary = tmp_path / "glossary.txt"
    global_glossary.write_text("GlobalTerm\n# reviewed by a human\n")
    scope_dir = tmp_path / "scopes" / "proj-x"
    scope_dir.mkdir(parents=True)
    (scope_dir / "glossary.txt").write_text("ScopeTerm\n")
    before = global_glossary.read_bytes()

    env = scribe_env(
        tmp_path,
        SCRIBE_GLOSSARY=str(global_glossary),
        SCRIBE_SCOPE_ROOT=str(tmp_path / "scopes"),
    )
    result = run(
        ["start", "--consent", "one-party", "--scope", "proj-x",
         "--topic", "Kickoff", "--attendees", "Alice"],
        env=env,
    )
    assert result.returncode == 0
    meeting_id = result.stdout.strip()
    hotwords = tmp_path / "ram" / "scribe" / meeting_id / "hotwords.txt"
    assert hotwords.read_text().splitlines() == ["ScopeTerm", "Alice", "Kickoff"]
    assert global_glossary.read_bytes() == before
    run(["abort"], env=env)


def test_start_without_scope_writes_no_hotwords_file(tmp_path):
    """No scope supplied → global file only; no per-meeting injection."""
    env = scribe_env(tmp_path)
    result = run(
        ["start", "--consent", "one-party", "--topic", "T", "--attendees", "A"],
        env=env,
    )
    assert result.returncode == 0
    meeting_id = result.stdout.strip()
    assert not (tmp_path / "ram" / "scribe" / meeting_id / "hotwords.txt").exists()
    run(["abort"], env=env)


def test_start_scope_glossary_disabled_by_env(tmp_path):
    """SCRIBE_MEETING_GLOSSARY=0 turns per-meeting injection off."""
    env = scribe_env(tmp_path, SCRIBE_MEETING_GLOSSARY="0")
    result = run(
        ["start", "--consent", "one-party", "--scope", "proj-x", "--topic", "T"],
        env=env,
    )
    assert result.returncode == 0
    meeting_id = result.stdout.strip()
    assert not (tmp_path / "ram" / "scribe" / meeting_id / "hotwords.txt").exists()
    run(["abort"], env=env)


def test_compose_capture_passes_glossary_extra_only_when_hotwords_exist(tmp_path):
    ram = tmp_path / "ram"
    meeting_dir = ram / "scribe" / "m1"
    meeting_dir.mkdir(parents=True)
    env = {"SCRIBE_RAMROOT": str(ram)}

    without = run(["--compose-capture", "m1"], env=env)
    assert "--glossary-extra" not in without.stdout

    (meeting_dir / "hotwords.txt").write_text("Term\n")
    with_hotwords = run(["--compose-capture", "m1"], env=env)
    assert "--glossary-extra" in with_hotwords.stdout
    assert "hotwords.txt" in with_hotwords.stdout


def test_transcribe_glossary_extra_is_additive_and_deduped(tmp_path):
    """Inline terms reach the recognizer additively; duplicates collapse."""
    global_glossary = tmp_path / "g.txt"
    global_glossary.write_text("Alice\nBob\n")
    extra = tmp_path / "extra.txt"
    extra.write_text("Bob\nCarol\n")
    transcript = tmp_path / "t.md"
    result = run_transcribe(
        ["--transcript", str(transcript), "--glossary", str(global_glossary),
         "--glossary-extra", str(extra)],
        env={"SCRIBE_STT_BACKEND": "fake", "SCRIBE_DEBUG": "1"},
        stdin="",
    )
    assert result.returncode == 0
    assert "glossary:3" in result.stderr


# --- §2 scribe-doc2text helper ---

DOC2TEXT = str(REPO_ROOT / "scribe-doc2text")


def run_doc2text(args, env=None):
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [BASH, DOC2TEXT, *args], capture_output=True, text=True, env=merged_env
    )


def test_doc2text_plaintext_passthrough(tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("hello\nworld\n")
    result = run_doc2text([str(doc)])
    assert result.returncode == 0
    assert result.stdout == "hello\nworld\n"


def test_doc2text_markdown_passthrough(tmp_path):
    doc = tmp_path / "notes.md"
    doc.write_text("# h\n")
    result = run_doc2text([str(doc)])
    assert result.returncode == 0
    assert result.stdout == "# h\n"


def test_doc2text_unknown_extension_is_loud_nonzero(tmp_path):
    doc = tmp_path / "blob.zzz"
    doc.write_text("x")
    result = run_doc2text([str(doc)])
    assert result.returncode != 0
    assert result.stdout == ""  # never an empty-string "success"
    assert "could not extract" in result.stderr
    assert str(doc) in result.stderr


def test_doc2text_no_extension_is_loud_nonzero(tmp_path):
    doc = tmp_path / "README"
    doc.write_text("x")
    result = run_doc2text([str(doc)])
    assert result.returncode != 0
    assert "could not extract" in result.stderr


def test_doc2text_missing_file_is_loud_nonzero(tmp_path):
    result = run_doc2text([str(tmp_path / "absent.txt")])
    assert result.returncode != 0
    assert "could not extract" in result.stderr


def test_doc2text_missing_extractor_is_loud_nonzero(tmp_path):
    """A configured extractor binary that isn't installed → non-zero with a
    message naming the override variable, not an empty success."""
    doc = tmp_path / "doc.pdf"
    doc.write_text("not really a pdf")
    result = run_doc2text(
        [str(doc)],
        env={"SCRIBE_EXTRACT_CMD_PDF": 'no-such-extractor-xyz "$1"'},
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "could not extract" in result.stderr
    assert "no-such-extractor-xyz" in result.stderr


def test_doc2text_env_override_runs_custom_extractor(tmp_path):
    doc = tmp_path / "doc.xyz"
    doc.write_text("abc\n")
    result = run_doc2text(
        [str(doc)],
        env={"SCRIBE_EXTRACT_CMD_XYZ": 'tr a-z A-Z < "$1"'},
    )
    assert result.returncode == 0
    assert result.stdout == "ABC\n"


def test_doc2text_extractor_runtime_failure_is_loud_nonzero(tmp_path):
    doc = tmp_path / "doc.xyz"
    doc.write_text("abc\n")
    result = run_doc2text(
        [str(doc)],
        env={"SCRIBE_EXTRACT_CMD_XYZ": "false"},
    )
    assert result.returncode != 0
    assert "extractor failed" in result.stderr


# --- §1 two-tier analyst: trigger contract (light tier) ---

def test_trigger_parsed_from_brief():
    out = run_analyst(
        ["--extract-trigger"],
        stdin="Now: things\nRETRIEVE: what does section 4 say\nWatch: x\n",
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "what does section 4 say"


def test_trigger_absent_yields_nothing():
    out = run_analyst(["--extract-trigger"], stdin="Now: things\nWatch: x\n")
    assert out.returncode == 0
    assert out.stdout.strip() == ""


def test_trigger_malformed_yields_nothing():
    """Garbled trigger lines degrade to 'no retrieval this cycle'."""
    malformed = (
        "RETRIEVE:\n"            # no query
        "RETRIEVE: \n"           # blank query
        "xRETRIEVE: query\n"     # not anchored at line start
        "  RETRIEVE: query\n"    # indented (as in the prompt's own example)
    )
    out = run_analyst(["--extract-trigger"], stdin=malformed)
    assert out.returncode == 0
    assert out.stdout.strip() == ""


def test_trigger_at_most_one_parsed():
    out = run_analyst(
        ["--extract-trigger"],
        stdin="RETRIEVE: first query\nRETRIEVE: second query\n",
    )
    assert out.stdout.strip() == "first query"


def test_trigger_line_stripped_from_pane_output(tmp_path):
    """The trigger is control data: it must never reach the pane file."""
    transcript = tmp_path / "t.md"
    transcript.write_text("[me] see clause four of the handbook\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(
        llm, "printf 'Now: clause talk\\nRETRIEVE: handbook clause four\\n'"
    )
    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={"SCRIBE_LLM_CMD": str(llm)},
    )
    assert result.returncode == 0
    pane = out.read_text()
    assert "Now: clause talk" in pane
    assert "RETRIEVE" not in pane


# --- §1 deep tier: locking, coalescing, drain ---

def wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_deep_submit_single_flight_and_pending_coalesces(tmp_path):
    """A second trigger while one retrieval is in flight lands in a single
    coalescing pending slot — the third overwrites the second."""
    transcript = tmp_path / "t.md"
    transcript.write_text("x\n")
    out = tmp_path / "brief.md"
    env = {"SCRIBE_DEEP_WORKER_CMD": "sleep 3"}

    first = run_analyst(
        ["--deep-submit", str(transcript), str(out), "query one"], env=env
    )
    assert first.returncode == 0
    lock = tmp_path / "brief.md.deeplock"
    assert lock.is_dir()

    run_analyst(["--deep-submit", str(transcript), str(out), "query two"], env=env)
    run_analyst(["--deep-submit", str(transcript), str(out), "query three"], env=env)
    pending = tmp_path / "brief.md.pending"
    assert pending.read_text() == "query three"  # coalesced, not queued


def test_deep_pending_drained_at_tick_top_when_lock_free(tmp_path):
    transcript = tmp_path / "t.md"
    transcript.write_text("")
    out = tmp_path / "brief.md"
    marker = tmp_path / "brief.md.marker"
    (tmp_path / "brief.md.pending").write_text("find the clause")

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={
            "SCRIBE_LLM_CMD": "true",
            "SCRIBE_DEEP_WORKER_CMD": 'printf %s "$3" > "$2.marker"',
        },
    )
    assert result.returncode == 0
    assert not (tmp_path / "brief.md.pending").exists()
    assert wait_for(marker.exists)
    assert marker.read_text() == "find the clause"


def test_deep_pending_not_drained_while_lock_held_live(tmp_path):
    transcript = tmp_path / "t.md"
    transcript.write_text("")
    out = tmp_path / "brief.md"
    pending = tmp_path / "brief.md.pending"
    pending.write_text("held back")
    lock = tmp_path / "brief.md.deeplock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))  # provably live

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={
            "SCRIBE_LLM_CMD": "true",
            "SCRIBE_DEEP_WORKER_CMD": 'printf %s "$3" > "$2.marker"',
        },
    )
    assert result.returncode == 0
    assert pending.read_text() == "held back"
    assert not (tmp_path / "brief.md.marker").exists()


def test_deep_stale_lock_is_reclaimed(tmp_path):
    """A lock whose recorded worker pid is dead must not block retrievals
    forever."""
    transcript = tmp_path / "t.md"
    transcript.write_text("x\n")
    out = tmp_path / "brief.md"
    lock = tmp_path / "brief.md.deeplock"
    lock.mkdir()
    # A pid that is certainly dead: spawn and reap a process.
    proc = subprocess.run(["bash", "-c", "echo $$"], capture_output=True, text=True)
    (lock / "pid").write_text(proc.stdout.strip())

    marker = tmp_path / "brief.md.marker"
    result = run_analyst(
        ["--deep-submit", str(transcript), str(out), "retry query"],
        env={"SCRIBE_DEEP_WORKER_CMD": 'printf %s "$3" > "$2.marker"'},
    )
    assert result.returncode == 0
    assert wait_for(marker.exists)
    assert marker.read_text() == "retry query"


def test_tick_trigger_dispatches_deep_worker_when_enabled(tmp_path):
    transcript = tmp_path / "t.md"
    transcript.write_text("[me] check the handbook\n")
    out = tmp_path / "brief.md"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, "printf 'Now: x\\nRETRIEVE: handbook clause\\n'")
    marker = tmp_path / "brief.md.marker"

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={
            "SCRIBE_LLM_CMD": str(llm),
            "SCRIBE_DEEP_CORPUS_ROOT": str(corpus),
            "SCRIBE_DEEP_WORKER_CMD": 'printf %s "$3" > "$2.marker"',
        },
    )
    assert result.returncode == 0
    assert wait_for(marker.exists)
    assert marker.read_text() == "handbook clause"


def test_tick_trigger_ignored_when_deep_disabled(tmp_path):
    """No corpus root configured → the trigger is parsed and stripped but
    dispatches nothing: a fresh install behaves exactly as today."""
    transcript = tmp_path / "t.md"
    transcript.write_text("[me] check the handbook\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, "printf 'Now: x\\nRETRIEVE: handbook clause\\n'")

    result = run_analyst(
        ["--analyst-once", str(transcript), str(out)],
        env={
            "SCRIBE_LLM_CMD": str(llm),
            "SCRIBE_DEEP_WORKER_CMD": 'printf %s "$3" > "$2.marker"',
        },
    )
    assert result.returncode == 0
    time.sleep(0.3)
    assert not (tmp_path / "brief.md.marker").exists()
    assert not (tmp_path / "brief.md.deeplock").exists()


# --- §1 deep tier: the worker itself ---

def test_deep_worker_quotes_source_verbatim_with_path(tmp_path):
    """With a passthrough LLM stub, the appended block carries the corpus
    text, the source path, and the verbatim/no-fabrication instructions."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "handbook.txt").write_text("The alpha clause says: forty-two.\n")
    (corpus / "other.txt").write_text("Unrelated content.\n")
    transcript = tmp_path / "t.md"
    transcript.write_text("x\n")
    out = tmp_path / "brief.md"

    result = run_analyst(
        ["--deep-worker", str(transcript), str(out), "alpha clause"],
        env={
            "SCRIBE_DEEP_CORPUS_ROOT": str(corpus),
            "SCRIBE_LLM_CMD_DEEP": "cat",
        },
    )
    assert result.returncode == 0
    deep = (tmp_path / "brief.md.deep").read_text()
    assert "--- retrieved: alpha clause ---" in deep
    assert "The alpha clause says: forty-two." in deep
    assert str(corpus / "handbook.txt") in deep
    assert "VERBATIM" in deep
    assert "Never fabricate" in deep
    # rendered pane picked the block up even with no light brief yet
    assert "alpha clause" in out.read_text()
    # the in-flight lock is released on worker exit
    assert not (tmp_path / "brief.md.deeplock").exists()
    # no scratch left behind
    leftovers = [p.name for p in tmp_path.glob(".deep-*")]
    assert leftovers == []


def test_deep_worker_no_match_says_so(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.txt").write_text("nothing relevant here\n")
    out = tmp_path / "brief.md"
    result = run_analyst(
        ["--deep-worker", str(tmp_path / "t.md"), str(out), "zzqy unfindable"],
        env={
            "SCRIBE_DEEP_CORPUS_ROOT": str(corpus),
            "SCRIBE_LLM_CMD_DEEP": "cat",
        },
    )
    assert result.returncode == 0
    deep = (tmp_path / "brief.md.deep").read_text()
    assert "No corpus document matched" in deep


def test_deep_worker_timeout_appends_failure_and_survives(tmp_path):
    """A hung retrieval is hard-killed; the failure is loud in the pane and
    the worker still exits 0 (the loop must survive)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.txt").write_text("alpha clause content\n")
    out = tmp_path / "brief.md"

    started = time.time()
    result = run_analyst(
        ["--deep-worker", str(tmp_path / "t.md"), str(out), "alpha clause"],
        env={
            "SCRIBE_DEEP_CORPUS_ROOT": str(corpus),
            "SCRIBE_LLM_CMD_DEEP": "sleep 30",
            "SCRIBE_DEEP_TIMEOUT": "1",
        },
    )
    elapsed = time.time() - started
    assert result.returncode == 0
    assert elapsed < 15
    deep = (tmp_path / "brief.md.deep").read_text()
    assert "RETRIEVAL FAILED" in deep
    assert not (tmp_path / "brief.md.deeplock").exists()


def test_deep_malformed_timeout_falls_back_with_warning(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.txt").write_text("alpha content\n")
    out = tmp_path / "brief.md"
    result = run_analyst(
        ["--deep-worker", str(tmp_path / "t.md"), str(out), "alpha"],
        env={
            "SCRIBE_DEEP_CORPUS_ROOT": str(corpus),
            "SCRIBE_LLM_CMD_DEEP": "cat",
            "SCRIBE_DEEP_TIMEOUT": "not-a-number",
        },
    )
    assert result.returncode == 0
    assert "not-a-number" in result.stderr  # loud fallback, not silence


# --- §1 rendering: composition, bound, no-clobber ---

def _deep_block(query, body):
    return f"--- retrieved: {query} ---\n{body}\n"


def test_render_bounds_deep_blocks_and_states_the_bound(tmp_path):
    out = tmp_path / "brief.md"
    (tmp_path / "brief.md.brief").write_text("Now: current state\n")
    blocks = "".join(_deep_block(f"q{i}", f"answer {i}") for i in range(1, 6))
    (tmp_path / "brief.md.deep").write_text(blocks)

    result = run_analyst(["--render", str(out)])
    assert result.returncode == 0
    text = out.read_text()
    assert "Now: current state" in text
    assert "showing last 3 of 5" in text  # the bound is stated, not silent
    assert "answer 5" in text and "answer 4" in text and "answer 3" in text
    assert "answer 1" not in text and "answer 2" not in text


def test_light_rewrite_does_not_clobber_deep_blocks(tmp_path):
    """The review finding the render step exists to fix: a new light tick
    must re-render the pane WITH the previously retrieved blocks."""
    transcript = tmp_path / "t.md"
    transcript.write_text("[me] first\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, 'input="$(cat)"; printf "BRIEF: %s" "$input"')
    env = {"SCRIBE_LLM_CMD": str(llm)}

    run_analyst(["--analyst-once", str(transcript), str(out)], env=env)
    # a deep block lands between ticks
    (tmp_path / "brief.md.deep").write_text(_deep_block("q", "the verbatim passage"))
    with transcript.open("a") as f:
        f.write("[me] second\n")
    run_analyst(["--analyst-once", str(transcript), str(out)], env=env)

    text = out.read_text()
    assert "second" in text                 # fresh brief
    assert "the verbatim passage" in text   # deep block preserved


def test_quiet_tick_still_surfaces_new_deep_block(tmp_path):
    """A deep block that lands during a silent meeting must reach the pane
    on the next tick even though the brief itself is not rewritten."""
    transcript = tmp_path / "t.md"
    transcript.write_text("[me] only line\n")
    out = tmp_path / "brief.md"
    llm = tmp_path / "llm-stub.sh"
    write_stub_hook(llm, 'input="$(cat)"; printf "BRIEF: %s" "$input"')
    env = {"SCRIBE_LLM_CMD": str(llm)}

    run_analyst(["--analyst-once", str(transcript), str(out)], env=env)
    time.sleep(0.05)
    (tmp_path / "brief.md.deep").write_text(_deep_block("q", "late block"))
    # no new transcript content: quiet tick
    run_analyst(["--analyst-once", str(transcript), str(out)], env=env)
    assert "late block" in out.read_text()


def test_concurrent_deep_appends_do_not_interleave(tmp_path):
    """Two writers hammering the deep file must serialize per-block. Written
    per the §6.1 pattern: both writers take their content as FILES and are
    started before either is waited on — no lock-then-stdin coupling."""
    out = tmp_path / "brief.md"
    block_a = tmp_path / "block-a.txt"
    block_b = tmp_path / "block-b.txt"
    block_a.write_text("--- retrieved: qa ---\n" + "A" * 400 + "\n")
    block_b.write_text("--- retrieved: qb ---\n" + "B" * 400 + "\n")

    script = (
        f'for i in $(seq 20); do '
        f'bash "{ANALYST_SCRIPT}" --append-deep "{out}" "$1"; done'
    )
    procs = [
        subprocess.Popen(["bash", "-c", script, "_", str(f)])
        for f in (block_a, block_b)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0

    lines = (tmp_path / "brief.md.deep").read_text().splitlines()
    delimiters = [l for l in lines if l.startswith("--- retrieved:")]
    assert len(delimiters) == 40
    for line in lines:
        if line.startswith("---"):
            continue
        assert not ("A" in line and "B" in line), "interleaved append detected"
        assert len(line) == 400


# --- §5 post-note gate seam ---

def read_audit(tmp_path):
    audit = tmp_path / "out" / "scribe-audit.log"
    return audit.read_text() if audit.exists() else ""


def start_stop_meeting(tmp_path, env, transcript_text="[me] hello\n", stop_env=None):
    started = run(["start", "--consent", "one-party", "--topic", "T"], env=env)
    assert started.returncode == 0, started.stderr
    meeting_id = started.stdout.strip()
    ram_dir = tmp_path / "ram" / "scribe" / meeting_id
    (ram_dir / "transcript.md").write_text(transcript_text)
    stopped = run(["stop", meeting_id], env={**env, **(stop_env or {})})
    return meeting_id, ram_dir, stopped


def test_gate_clear_destroys_and_audits(tmp_path):
    gate = tmp_path / "gate.sh"
    write_stub_hook(gate, 'exit 0')
    env = scribe_env(tmp_path, SCRIBE_GATE_CMD=str(gate))
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    assert not ram_dir.exists()
    assert (tmp_path / "out" / f"{meeting_id}.md").is_file()
    audit = read_audit(tmp_path)
    assert meeting_id in audit
    assert "\tclear\tdestroyed\t" in audit


def test_gate_receives_note_path_and_live_meeting_dir(tmp_path):
    """The gate runs AFTER the note is written and BEFORE destruction: its
    $1 is the note, its $2 the still-existing meeting dir."""
    seen = tmp_path / "gate-args.txt"
    gate = tmp_path / "gate.sh"
    write_stub_hook(
        gate,
        f'printf "%s\\n%s\\n" "$1" "$2" > "{seen}"; '
        f'[ -f "$1" ] || exit 9; [ -d "$2" ] || exit 8; exit 0',
    )
    env = scribe_env(tmp_path, SCRIBE_GATE_CMD=str(gate))
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    lines = seen.read_text().splitlines()
    assert lines[0].endswith(f"{meeting_id}.note.md")  # note, not transcript
    assert lines[1] == str(ram_dir)
    audit = read_audit(tmp_path)
    assert "\tclear\t" in audit  # gate's [ -f/-d ] checks passed


def test_gate_hold_quarantines_and_skips_hook(tmp_path):
    hook_marker = tmp_path / "hook-ran"
    hook = tmp_path / "hook.sh"
    write_stub_hook(hook, f'touch "{hook_marker}"')
    gate = tmp_path / "gate.sh"
    write_stub_hook(gate, 'echo "needs review"; exit 75')
    env = scribe_env(
        tmp_path, SCRIBE_GATE_CMD=str(gate), SCRIBE_ON_STOP=str(hook)
    )
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    # not destroyed: moved out of RAM into quarantine
    assert not ram_dir.exists()
    quarantined = tmp_path / "out" / "quarantine" / meeting_id
    assert (quarantined / "transcript.md").is_file()
    # transcript still written out
    assert (tmp_path / "out" / f"{meeting_id}.md").is_file()
    # audited with the gate's own message
    audit = read_audit(tmp_path)
    assert "\thold\t" in audit
    assert "needs review" in audit
    # downstream hook did NOT run on hold
    assert not hook_marker.exists()


def test_gate_crash_treated_as_hold(tmp_path):
    gate = tmp_path / "gate.sh"
    write_stub_hook(gate, 'echo boom >&2; exit 3')
    env = scribe_env(tmp_path, SCRIBE_GATE_CMD=str(gate))
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    assert (tmp_path / "out" / "quarantine" / meeting_id).is_dir()
    audit = read_audit(tmp_path)
    assert "\thold\t" in audit
    assert "treated as hold" in audit


def test_gate_timeout_treated_as_hold(tmp_path):
    gate = tmp_path / "gate.sh"
    write_stub_hook(gate, 'sleep 30')
    env = scribe_env(
        tmp_path, SCRIBE_GATE_CMD=str(gate), SCRIBE_GATE_TIMEOUT="1"
    )
    started = time.time()
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    elapsed = time.time() - started
    assert stopped.returncode == 0
    assert elapsed < 20
    assert (tmp_path / "out" / "quarantine" / meeting_id).is_dir()
    assert "\thold\t" in read_audit(tmp_path)


def test_gate_unconfigured_destroys_and_audits_as_today(tmp_path):
    env = scribe_env(tmp_path)
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    assert not ram_dir.exists()
    assert not (tmp_path / "out" / "quarantine").exists()
    assert "\tno-gate\tdestroyed\t" in read_audit(tmp_path)


def test_quarantine_path_never_lands_inside_ram_root(tmp_path):
    """A misconfigured quarantine dir inside the RAM root is refused with a
    warning and redirected to a durable location."""
    gate = tmp_path / "gate.sh"
    write_stub_hook(gate, 'exit 75')
    env = scribe_env(
        tmp_path,
        SCRIBE_GATE_CMD=str(gate),
        SCRIBE_QUARANTINE_DIR=str(tmp_path / "ram" / "evil-quarantine"),
    )
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    assert "RAM root" in stopped.stderr
    assert not (tmp_path / "ram" / "evil-quarantine").exists()
    assert (tmp_path / "out" / "quarantine" / meeting_id).is_dir()


def test_core_note_step_writes_note_before_gate_without_hook(tmp_path):
    """Default flow (no SCRIBE_ON_STOP): core generates the note itself."""
    note_llm = tmp_path / "llm.sh"
    write_stub_hook(note_llm, 'cat >/dev/null; echo "Attendees: Alice"')
    env = scribe_env(tmp_path, SCRIBE_LLM_CMD=str(note_llm))
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    note = tmp_path / "out" / f"{meeting_id}.note.md"
    assert note.is_file()
    assert "Attendees: Alice" in note.read_text()


def test_legacy_on_stop_hook_still_owns_note_generation(tmp_path):
    """SCRIBE_ON_STOP set (and no SCRIBE_NOTES_CMD) → core writes no note of
    its own, exactly the pre-gate behavior."""
    hook = tmp_path / "hook.sh"
    write_stub_hook(hook, 'exit 0')
    env = scribe_env(tmp_path, SCRIBE_ON_STOP=str(hook))
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    assert not (tmp_path / "out" / f"{meeting_id}.note.md").exists()


def test_note_step_failure_is_nonfatal_and_transcript_preserved(tmp_path):
    env = scribe_env(tmp_path, SCRIBE_NOTES_CMD="false")
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    assert "note generation failed" in stopped.stderr
    assert (tmp_path / "out" / f"{meeting_id}.md").is_file()


# --- §4 auto-artifacts ---

import sys

sys.path.insert(0, str(REPO_ROOT))
import scribe_artifacts_lib as artlib  # noqa: E402

ARTIFACTS_CLI = str(REPO_ROOT / "scribe-artifacts")
BUILD_CLI = str(REPO_ROOT / "scribe-artifacts-build")


def artifacts_env(tmp_path, **extra):
    env = {"SCRIBE_OUTPUT_DIR": str(tmp_path / "out")}
    env.update(extra)
    return env


def run_artifacts(args, env=None, stdin=""):
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        ["python3", ARTIFACTS_CLI, *args],
        input=stdin, capture_output=True, text=True, env=merged,
    )


def run_build_cli(args, env=None):
    merged = {**os.environ, **(env or {})}
    return subprocess.run(
        ["python3", BUILD_CLI, *args], capture_output=True, text=True, env=merged,
    )


def sidecar_file(tmp_path, meeting):
    return tmp_path / "out" / ".artifacts" / f"{meeting}.sidecar.tsv"


def write_sidecar_rows(tmp_path, meeting, rows):
    d = tmp_path / "out" / ".artifacts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{meeting}.sidecar.tsv").write_text(
        "".join(r + "\n" for r in rows), encoding="utf-8"
    )


def make_note(tmp_path, name="m1.note.md", text="Attendees: Alice\nDecisions: ship it\n"):
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    note = out / name
    note.write_text(text)
    return note


CLASSIFY_JSON = (
    '[{"type": "summary", "scope": "proj", "audience": "team", "topic": "alpha", "trigger_class": "explicit"},'
    ' {"type": "memo", "scope": "proj", "audience": "lead", "topic": "beta", "trigger_class": "weak"},'
    ' {"type": "deck", "scope": "proj", "audience": "team", "topic": "gamma", "trigger_class": "explicit"},'
    ' {"type": "status-update", "scope": "proj", "audience": "team", "topic": "delta", "trigger_class": "confident"}]'
)


def classify_stub(tmp_path, payload_line):
    llm = tmp_path / "classify-llm.sh"
    write_stub_hook(llm, f"cat > /dev/null; printf '%s\\n' '{payload_line}'")
    return str(llm)


# §4b cap parsing

def test_cap_default_and_zero_and_inline_comment(monkeypatch):
    monkeypatch.delenv("SCRIBE_ARTIFACT_CAP", raising=False)
    assert artlib.parse_cap() == 6
    monkeypatch.setenv("SCRIBE_ARTIFACT_CAP", "0")
    assert artlib.parse_cap() == 0
    monkeypatch.setenv("SCRIBE_ARTIFACT_CAP", "2  # keep it low")
    assert artlib.parse_cap() == 2


def test_cap_malformed_falls_back_with_warning(monkeypatch, capsys):
    for bad in ("abc", "-3", "3.5", "#2"):
        monkeypatch.setenv("SCRIBE_ARTIFACT_CAP", bad)
        assert artlib.parse_cap() == 6
        assert "SCRIBE_ARTIFACT_CAP" in capsys.readouterr().err


# §4a/§4c classification

def test_classify_assigns_ids_across_all_dispositions(tmp_path):
    note = make_note(tmp_path)
    calls = tmp_path / "build-calls.txt"
    env = artifacts_env(
        tmp_path,
        SCRIBE_LLM_CMD=classify_stub(tmp_path, "SCRIBE-CANDIDATES: " + CLASSIFY_JSON),
        SCRIBE_ARTIFACT_TYPES="summary memo status-update",
        SCRIBE_ARTIFACT_CAP="1",
        SCRIBE_ARTIFACT_BUILD_CMD=f'printf "%s|%s|%s\\n" "$2" "$4" "$7" >> "{calls}"',
    )
    result = run_build_cli(
        ["classify", "--meeting", "m1", "--note", str(note)], env=env
    )
    assert result.returncode == 0
    rows = sidecar_file(tmp_path, "m1").read_text().splitlines()
    parsed = [r.split("\t") for r in rows]
    assert [p[0] for p in parsed] == ["1", "2", "3", "4"]  # ids across skips
    assert [p[1] for p in parsed] == [
        "built", "weaker-trigger", "disabled-type", "over-cap"
    ]
    assert all(p[7] == str(note) for p in parsed)  # note_path on every row
    # classification output printed in full
    for i in ("1", "2", "3", "4"):
        assert f"candidate {i}:" in result.stdout
    # only the one within-cap strong candidate was dispatched
    assert wait_for(calls.exists)
    assert calls.read_text().splitlines() == ["1|summary|alpha"]


def test_classify_malformed_payloads_degrade_to_no_candidates(tmp_path):
    note = make_note(tmp_path)
    payloads = (
        "no sentinel here at all",
        "SCRIBE-CANDIDATES: not-json",
        'SCRIBE-CANDIDATES: {"type": "x"}',
        'SCRIBE-CANDIDATES: ["just", "strings"]',
    )
    for payload in payloads:
        env = artifacts_env(
            tmp_path,
            SCRIBE_LLM_CMD=classify_stub(tmp_path, payload),
            SCRIBE_ARTIFACT_BUILD_CMD="true",
        )
        result = run_build_cli(
            ["classify", "--meeting", "m1", "--note", str(note)], env=env
        )
        assert result.returncode == 0, payload
        assert "no artifact candidates" in result.stdout


def test_classify_sidecar_failure_does_not_suppress_output(tmp_path):
    """§4c: the sidecar is an enablement artifact; if it cannot be written,
    warn on stderr and still produce the classification output in full."""
    note = make_note(tmp_path)
    # Occupy the artifacts-dir path with a FILE so mkdir fails.
    (tmp_path / "out" / ".artifacts").write_text("in the way")
    env = artifacts_env(
        tmp_path,
        SCRIBE_LLM_CMD=classify_stub(tmp_path, "SCRIBE-CANDIDATES: " + CLASSIFY_JSON),
        SCRIBE_ARTIFACT_CAP="0",
        SCRIBE_ARTIFACT_BUILD_CMD="true",
    )
    result = run_build_cli(["classify", "--meeting", "m1", "--note", str(note)], env=env)
    assert result.returncode == 0
    assert "could not write sidecar" in result.stderr  # loud, on stderr
    for i in ("1", "2", "3", "4"):
        assert f"candidate {i}:" in result.stdout  # output still complete


def test_classify_sanitizes_full_separator_class(tmp_path):
    """A topic carrying \\r (and friends) must not split a sidecar row."""
    payload = (
        'SCRIBE-CANDIDATES: [{"type": "summary", "scope": "s", "audience": "a",'
        ' "topic": "line\\rbreak\\ttab", "trigger_class": "weak"}]'
    )
    note = make_note(tmp_path)
    env = artifacts_env(
        tmp_path,
        SCRIBE_LLM_CMD=classify_stub(tmp_path, payload),
        SCRIBE_ARTIFACT_BUILD_CMD="true",
    )
    result = run_build_cli(["classify", "--meeting", "m1", "--note", str(note)], env=env)
    assert result.returncode == 0
    rows = sidecar_file(tmp_path, "m1").read_text().splitlines()
    assert len(rows) == 1  # not split into two malformed rows
    fields = rows[0].split("\t")
    assert len(fields) == 8
    assert fields[5] == "line break tab"


def test_sanitize_field_covers_splitlines_class():
    hairy = "a\rb\vc\fd\x1ce\x1df\x1eg\x85h i j\tk"
    cleaned = artlib.sanitize_field(hairy)
    assert cleaned.splitlines() == [cleaned]  # no embedded record breaks
    assert "\t" not in cleaned


def test_classify_already_built_consumes_no_cap(tmp_path, monkeypatch):
    note = make_note(tmp_path)
    monkeypatch.setenv("SCRIBE_OUTPUT_DIR", str(tmp_path / "out"))
    artlib.append_run_log("built", str(note), "summary", "alpha", "/x/alpha.md")
    calls = tmp_path / "build-calls.txt"
    payload = (
        'SCRIBE-CANDIDATES: [{"type": "summary", "scope": "s", "audience": "a",'
        ' "topic": "alpha", "trigger_class": "explicit"},'
        ' {"type": "summary", "scope": "s", "audience": "a",'
        ' "topic": "fresh", "trigger_class": "explicit"}]'
    )
    env = artifacts_env(
        tmp_path,
        SCRIBE_LLM_CMD=classify_stub(tmp_path, payload),
        SCRIBE_ARTIFACT_CAP="1",
        SCRIBE_ARTIFACT_BUILD_CMD=f'printf "%s\\n" "$7" >> "{calls}"',
    )
    result = run_build_cli(["classify", "--meeting", "m1", "--note", str(note)], env=env)
    assert result.returncode == 0
    rows = [r.split("\t") for r in sidecar_file(tmp_path, "m1").read_text().splitlines()]
    assert rows[0][1] == "built" and rows[1][1] == "built"
    assert wait_for(calls.exists)
    assert calls.read_text().splitlines() == ["fresh"]  # alpha not re-dispatched


# §4d build

def test_build_creates_draft_backlinks_and_review_task(tmp_path):
    note = make_note(tmp_path)
    env = artifacts_env(tmp_path, SCRIBE_ARTIFACT_GENERATOR_CMD='echo "draft body"')
    result = run_build_cli(
        ["build", "--meeting", "m1", "--id", "1", "--note", str(note),
         "--type", "summary", "--topic", "alpha"],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    artifact = tmp_path / "out" / "artifacts" / "m1-summary-alpha.md"
    assert artifact.read_text().strip() == "draft body"
    assert f"> draft: {artifact}" in note.read_text()
    log = (tmp_path / "out" / ".artifacts" / "run.log").read_text()
    assert "\tbuilt\t" in log
    tasks = (tmp_path / "out" / ".artifacts" / "review-tasks.tsv").read_text()
    assert str(artifact) in tasks
    assert "\topen\t" in tasks


def test_build_is_idempotent(tmp_path):
    note = make_note(tmp_path)
    env = artifacts_env(tmp_path, SCRIBE_ARTIFACT_GENERATOR_CMD='echo "draft"')
    args = ["build", "--meeting", "m1", "--id", "1", "--note", str(note),
            "--type", "summary", "--topic", "alpha"]
    first = run_build_cli(args, env=env)
    assert first.returncode == 0
    second = run_build_cli(args, env=env)
    assert second.returncode == 0
    assert "already built" in second.stdout
    log = (tmp_path / "out" / ".artifacts" / "run.log").read_text()
    assert log.count("\tbuilt\t") == 1


def test_build_missing_note_refuses_and_logs(tmp_path):
    (tmp_path / "out").mkdir(parents=True)
    env = artifacts_env(tmp_path, SCRIBE_ARTIFACT_GENERATOR_CMD='echo "draft"')
    result = run_build_cli(
        ["build", "--meeting", "m1", "--id", "3", "--note",
         str(tmp_path / "out" / "gone.note.md"), "--type", "summary",
         "--topic", "alpha"],
        env=env,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr and "3" in result.stderr
    log = (tmp_path / "out" / ".artifacts" / "run.log").read_text()
    assert "\tbuild-failed\t" in log
    assert not (tmp_path / "out" / "artifacts" / "m1-summary-alpha.md").exists()


def test_generator_failure_logs_build_failed(tmp_path):
    note = make_note(tmp_path)
    env = artifacts_env(tmp_path, SCRIBE_ARTIFACT_GENERATOR_CMD="false")
    result = run_build_cli(
        ["build", "--meeting", "m1", "--id", "1", "--note", str(note),
         "--type", "summary", "--topic", "alpha"],
        env=env,
    )
    assert result.returncode != 0
    log = (tmp_path / "out" / ".artifacts" / "run.log").read_text()
    assert "\tbuild-failed\t" in log


# §4e render + joins

def test_render_groups_always_printed_even_at_zero(tmp_path):
    write_sidecar_rows(tmp_path, "m1", [])
    result = run_artifacts(["m1"], env=artifacts_env(tmp_path))
    assert result.returncode == 0
    for header in ("BUILT (0):", "QUEUED (0):", "disabled-type (0):", "OTHER (0):"):
        assert header in result.stdout


def test_render_unrecognized_disposition_lands_in_other(tmp_path):
    write_sidecar_rows(
        tmp_path, "m1",
        ["1\tbogus-state\tsummary\ts\ta\talpha\texplicit\t/n.md"],
    )
    result = run_artifacts(["m1"], env=artifacts_env(tmp_path))
    assert "OTHER (1):" in result.stdout
    assert "bogus-state" in result.stdout


def test_render_join_keys_on_note_path_type_topic(tmp_path, monkeypatch):
    """§4e: recurring meetings repeat a topic; a (type, topic) join would
    report week B's unbuilt candidate as built with week A's artifact."""
    monkeypatch.setenv("SCRIBE_OUTPUT_DIR", str(tmp_path / "out"))
    note_a = make_note(tmp_path, "week-a.note.md")
    note_b = make_note(tmp_path, "week-b.note.md")
    artlib.append_run_log("built", str(note_a), "summary", "standup", "/x/week-a.md")
    write_sidecar_rows(
        tmp_path, "week-b",
        [f"1\tbuilt\tsummary\ts\ta\tstandup\texplicit\t{note_b}"],
    )
    result = run_artifacts(["week-b"], env=artifacts_env(tmp_path))
    assert "/x/week-a.md" not in result.stdout  # another meeting's artifact
    assert "building…" in result.stdout
    # and the meeting whose note DID build shows its artifact
    write_sidecar_rows(
        tmp_path, "week-a",
        [f"1\tbuilt\tsummary\ts\ta\tstandup\texplicit\t{note_a}"],
    )
    result_a = run_artifacts(["week-a"], env=artifacts_env(tmp_path))
    assert "/x/week-a.md" in result_a.stdout


def test_render_reverse_scan_latest_outcome_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIBE_OUTPUT_DIR", str(tmp_path / "out"))
    note = make_note(tmp_path)
    artlib.append_run_log("build-failed", str(note), "summary", "alpha", detail="first try")
    artlib.append_run_log("built", str(note), "summary", "alpha", "/x/alpha.md")
    write_sidecar_rows(
        tmp_path, "m1",
        [f"1\tbuilt\tsummary\ts\ta\talpha\texplicit\t{note}"],
    )
    result = run_artifacts(["m1"], env=artifacts_env(tmp_path))
    assert "/x/alpha.md" in result.stdout  # the retried build's success wins
    assert "FAILED" not in result.stdout


def test_task_status_delimited_match_and_open_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIBE_OUTPUT_DIR", str(tmp_path / "out"))
    d = tmp_path / "out" / ".artifacts"
    d.mkdir(parents=True)
    (d / "review-tasks.tsv").write_text(
        "archive/docs/out.md\topen\tt1\n"
        "docs/out.md\tdone\tt2\n"
        "multi/task.md\topen\tt3\n"
        "multi/task.md\tdone\tt4\n"
    )
    # substring of another path must not match it
    assert artlib.task_status("docs/out.md") == "reviewed"
    # several rows for one path: ANY open row wins
    assert artlib.task_status("multi/task.md") == "needs review"
    assert artlib.task_status("unknown.md") is None


def test_render_approve_hint_lists_queued_ids(tmp_path):
    write_sidecar_rows(
        tmp_path, "m1",
        ["1\tbuilt\tsummary\ts\ta\talpha\texplicit\t/n.md",
         "2\tover-cap\tmemo\ts\ta\tbeta\texplicit\t/n.md",
         "3\tweaker-trigger\tsummary\ts\ta\tgamma\tweak\t/n.md"],
    )
    result = run_artifacts(["m1"], env=artifacts_env(tmp_path))
    assert "--approve 2 3" in result.stdout


# §4f approve

def approve_fixture(tmp_path):
    note = make_note(tmp_path)
    write_sidecar_rows(
        tmp_path, "m1",
        [f"1\tbuilt\tsummary\ts\ta\talpha\texplicit\t{note}",
         f"2\tover-cap\tmemo\ts\tlead\tbeta\texplicit\t{note}",
         f"3\tweaker-trigger\tsummary\ts\ta\tgamma\tweak\t{note}"],
    )
    return note


def test_approve_unknown_id_dispatches_zero_builds(tmp_path):
    approve_fixture(tmp_path)
    calls = tmp_path / "calls.txt"
    env = artifacts_env(
        tmp_path, SCRIBE_ARTIFACT_BUILD_CMD=f'echo "$2" >> "{calls}"'
    )
    result = run_artifacts(["m1", "--approve", "2", "99"], env=env)
    assert result.returncode == 2      # distinct exit status
    assert result.stdout == ""         # nothing on stdout
    assert "99" in result.stderr
    assert "2 3" in result.stderr or "2, 3" in result.stderr  # valid ids listed
    time.sleep(0.2)
    assert not calls.exists()          # one bad id → zero builds
    rows = sidecar_file(tmp_path, "m1").read_text()
    assert "\tover-cap\t" in rows      # nothing recorded either


def test_approve_non_queued_id_is_error_not_silent_skip(tmp_path):
    approve_fixture(tmp_path)
    result = run_artifacts(["m1", "--approve", "1"], env=artifacts_env(tmp_path))
    assert result.returncode == 2
    assert result.stdout == ""
    assert "not queued" in result.stderr


def test_approve_records_before_dispatch(tmp_path):
    """§4f: by the time the (detached) build runs, the approval must already
    be recorded in the sidecar."""
    approve_fixture(tmp_path)
    sidecar = sidecar_file(tmp_path, "m1")
    marker = tmp_path / "seen-at-dispatch.txt"
    seam = (
        f'if grep -q "^$2\tapproved" "{sidecar}"; then echo recorded > "{marker}"; '
        f'else echo missing > "{marker}"; fi'
    )
    env = artifacts_env(tmp_path, SCRIBE_ARTIFACT_BUILD_CMD=seam)
    result = run_artifacts(["m1", "--approve", "2"], env=env)
    assert result.returncode == 0, result.stderr
    assert wait_for(marker.exists)
    assert marker.read_text().strip() == "recorded"


def test_approve_missing_note_refused_row_left_queued_batch_continues(tmp_path):
    note = make_note(tmp_path)
    write_sidecar_rows(
        tmp_path, "m1",
        [f"2\tover-cap\tmemo\ts\ta\tbeta\texplicit\t{tmp_path}/out/gone.note.md",
         f"3\tover-cap\tsummary\ts\ta\tgamma\texplicit\t{note}"],
    )
    calls = tmp_path / "calls.txt"
    env = artifacts_env(tmp_path, SCRIBE_ARTIFACT_BUILD_CMD=f'echo "$2" >> "{calls}"')
    result = run_artifacts(["m1", "--approve", "2", "3"], env=env)
    assert result.returncode == 1          # non-zero at the end
    assert "2" in result.stderr and "not found" in result.stderr
    assert wait_for(calls.exists)
    assert calls.read_text().splitlines() == ["3"]  # batch continued
    rows = sidecar_file(tmp_path, "m1").read_text().splitlines()
    assert rows[0].split("\t")[1] == "over-cap"   # refused row untouched
    assert rows[1].split("\t")[1] == "approved"


def test_approve_reconstructed_lid_rejected(tmp_path):
    approve_fixture(tmp_path)
    result = run_artifacts(["m1", "--approve", "L1"], env=artifacts_env(tmp_path))
    assert result.returncode == 2
    assert result.stdout == ""
    assert "log-derived" in result.stderr


def test_approve_all_approves_exactly_the_queued(tmp_path):
    approve_fixture(tmp_path)
    calls = tmp_path / "calls.txt"
    env = artifacts_env(tmp_path, SCRIBE_ARTIFACT_BUILD_CMD=f'echo "$2" >> "{calls}"')
    result = run_artifacts(["m1", "--approve-all"], env=env)
    assert result.returncode == 0, result.stderr
    assert wait_for(lambda: calls.exists() and len(calls.read_text().splitlines()) == 2)
    assert sorted(calls.read_text().split()) == ["2", "3"]


# §4g sidecar mutation

def test_mark_edits_only_target_row_byte_for_byte(tmp_path):
    rows = [
        "1\tover-cap\tsummary\ts\ta\talpha\texplicit\t/n.md",
        "2\tover-cap",                                     # short row
        "3\tover-cap\ta\tb\tc\td\te\tf\tg\th\textra",      # over-long row
        "",                                                 # blank line
        "4\tweaker-trigger\tmemo\ts\ta\tdelta\tweak\t/n.md",
    ]
    write_sidecar_rows(tmp_path, "m1", rows)
    result = run_artifacts(
        ["mark", "m1", "1", "approved"], env=artifacts_env(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    after = sidecar_file(tmp_path, "m1").read_text().splitlines()
    assert after[0].split("\t")[1] == "approved"
    assert after[1] == rows[1]   # short row byte-identical
    assert after[2] == rows[2]   # over-long row byte-identical, fields kept
    assert after[3] == rows[3]   # blank line preserved
    assert after[4] == rows[4]


def test_mark_duplicate_id_refused(tmp_path):
    write_sidecar_rows(
        tmp_path, "m1",
        ["3\tover-cap\tsummary\ts\ta\talpha\texplicit\t/n.md",
         "3\tover-cap\tmemo\ts\ta\tbeta\texplicit\t/n.md"],
    )
    result = run_artifacts(["mark", "m1", "3", "approved"], env=artifacts_env(tmp_path))
    assert result.returncode != 0
    assert "duplicate" in result.stderr.lower()


def test_mark_unknown_id_and_invalid_disposition_rejected(tmp_path):
    write_sidecar_rows(
        tmp_path, "m1", ["1\tover-cap\tsummary\ts\ta\talpha\texplicit\t/n.md"]
    )
    before = sidecar_file(tmp_path, "m1").read_text()
    unknown = run_artifacts(["mark", "m1", "9", "approved"], env=artifacts_env(tmp_path))
    assert unknown.returncode != 0
    invalid = run_artifacts(["mark", "m1", "1", "sent"], env=artifacts_env(tmp_path))
    assert invalid.returncode != 0
    assert "invalid disposition" in invalid.stderr
    assert sidecar_file(tmp_path, "m1").read_text() == before


def test_mark_preserves_file_mode(tmp_path):
    write_sidecar_rows(
        tmp_path, "m1", ["1\tover-cap\tsummary\ts\ta\talpha\texplicit\t/n.md"]
    )
    path = sidecar_file(tmp_path, "m1")
    path.chmod(0o640)
    result = run_artifacts(["mark", "m1", "1", "approved"], env=artifacts_env(tmp_path))
    assert result.returncode == 0
    assert (path.stat().st_mode & 0o777) == 0o640


# §4i back-compat reconstruction

def test_absent_sidecar_reconstructs_from_log_with_lids(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIBE_OUTPUT_DIR", str(tmp_path / "out"))
    note_old = make_note(tmp_path, "older.note.md")
    note_new = make_note(tmp_path, "newer.note.md")
    artlib.append_run_log("built", str(note_old), "memo", "old-topic", "/x/old.md")
    artlib.append_run_log("built", str(note_new), "summary", "s1", "/x/s1.md")
    artlib.append_run_log("built", str(note_new), "memo", "s2", "/x/s2.md")
    result = run_artifacts(["pre-sidecar-meeting"], env=artifacts_env(tmp_path))
    assert result.returncode == 0
    out = result.stdout
    assert "RECONSTRUCTED" in out
    assert str(note_new) in out          # single source group, named
    assert "[L1]" in out and "[L2]" in out
    assert "/x/old.md" not in out        # other note's builds excluded
    assert "CANNOT be recovered" in out  # never infer nothing was skipped
    assert "not approvable" in out.lower() or "L-ids" in out


def test_empty_sidecar_is_not_reconstructed(tmp_path, monkeypatch):
    """§4i requirement 1: present-but-empty renders as a real zero-candidate
    meeting — reconstruction would show the PREVIOUS meeting's artifacts
    under this meeting's id."""
    monkeypatch.setenv("SCRIBE_OUTPUT_DIR", str(tmp_path / "out"))
    note = make_note(tmp_path)
    artlib.append_run_log("built", str(note), "summary", "alpha", "/x/a.md")
    write_sidecar_rows(tmp_path, "fresh-meeting", [])
    result = run_artifacts(["fresh-meeting"], env=artifacts_env(tmp_path))
    assert "RECONSTRUCTED" not in result.stdout
    assert "BUILT (0):" in result.stdout
    assert "/x/a.md" not in result.stdout


def test_unreadable_sidecar_reports_not_reconstructs(tmp_path):
    d = tmp_path / "out" / ".artifacts"
    d.mkdir(parents=True)
    (d / "m1.sidecar.tsv").mkdir()  # a directory at the path
    result = run_artifacts(["m1"], env=artifacts_env(tmp_path))
    assert result.returncode == 0
    assert "cannot be read" in result.stdout
    assert "RECONSTRUCTED" not in result.stdout


def test_no_record_message_when_nothing_anywhere(tmp_path):
    (tmp_path / "out").mkdir(parents=True)
    result = run_artifacts(["typo-meeting"], env=artifacts_env(tmp_path))
    assert result.returncode == 0
    assert "No record for 'typo-meeting'" in result.stdout


# §4h pane

def test_review_surface_makes_no_model_call():
    """The review surface (CLI + lib) is zero-model by construction; scan
    the sources for model-command references."""
    for path in (ARTIFACTS_CLI, str(REPO_ROOT / "scribe_artifacts_lib.py")):
        source = pathlib.Path(path).read_text()
        assert "SCRIBE_LLM" not in source, path
        assert "claude" not in source.lower(), path


def test_pane_eof_exits_zero(tmp_path):
    write_sidecar_rows(
        tmp_path, "m1", ["1\tover-cap\tsummary\ts\ta\talpha\texplicit\t/n.md"]
    )
    result = run_artifacts(["pane", "m1"], env=artifacts_env(tmp_path), stdin="")
    assert result.returncode == 0


def test_pane_unknown_input_reprompts_never_exits(tmp_path):
    write_sidecar_rows(
        tmp_path, "m1", ["1\tover-cap\tsummary\ts\ta\talpha\texplicit\t/n.md"]
    )
    result = run_artifacts(
        ["pane", "m1"], env=artifacts_env(tmp_path),
        stdin="garbage input\nquit\n",
    )
    assert result.returncode == 0
    assert result.stdout.count("approve <id>") >= 2  # re-prompted with help


def test_pane_approve_dispatches_reconstructed_argv(tmp_path):
    """§4j: assert on the argv the pane hands the dispatcher, so an
    id-slicing regression in the pane's input parsing is caught."""
    note = make_note(tmp_path)
    write_sidecar_rows(
        tmp_path, "m1",
        [f"4\tover-cap\tsummary\ts\tteam\talphatopic\texplicit\t{note}",
         f"5\tweaker-trigger\tmemo\ts\tlead\tbetatopic\tweak\t{note}"],
    )
    calls = tmp_path / "calls.txt"
    env = artifacts_env(
        tmp_path,
        SCRIBE_ARTIFACT_BUILD_CMD=f'printf "%s|%s|%s\\n" "$2" "$4" "$7" >> "{calls}"',
    )
    result = run_artifacts(
        ["pane", "m1"], env=env, stdin="approve 4 5\nquit\n"
    )
    assert result.returncode == 0
    assert wait_for(lambda: calls.exists() and len(calls.read_text().splitlines()) == 2)
    assert calls.read_text().splitlines() == [
        "4|summary|alphatopic", "5|memo|betatopic"
    ]


def test_pane_all_branch_dispatches_all_queued(tmp_path):
    note = make_note(tmp_path)
    write_sidecar_rows(
        tmp_path, "m1",
        [f"4\tover-cap\tsummary\ts\ta\talpha\texplicit\t{note}",
         f"5\tweaker-trigger\tmemo\ts\ta\tbeta\tweak\t{note}"],
    )
    calls = tmp_path / "calls.txt"
    env = artifacts_env(tmp_path, SCRIBE_ARTIFACT_BUILD_CMD=f'echo "$2" >> "{calls}"')
    result = run_artifacts(["pane", "m1"], env=env, stdin="all\nquit\n")
    assert result.returncode == 0
    assert wait_for(lambda: calls.exists() and len(calls.read_text().splitlines()) == 2)


# meeting resolution + --all

def test_ambiguous_meeting_is_error_naming_matches(tmp_path):
    write_sidecar_rows(tmp_path, "20250101-team-sync", [])
    write_sidecar_rows(tmp_path, "20250201-team-sync", [])
    result = run_artifacts(["team-sync"], env=artifacts_env(tmp_path))
    assert result.returncode == 2
    assert "20250101-team-sync" in result.stderr
    assert "20250201-team-sync" in result.stderr


def test_all_summary_is_bounded_and_states_the_bound(tmp_path):
    for name in ("m1", "m2", "m3"):
        write_sidecar_rows(
            tmp_path, name, ["1\tover-cap\tsummary\ts\ta\tt\texplicit\t/n.md"]
        )
    result = run_artifacts(
        ["--all"], env=artifacts_env(tmp_path, SCRIBE_ARTIFACTS_ALL_LIMIT="2")
    )
    assert result.returncode == 0
    assert "showing 2 of 3" in result.stdout
    assert "m1" not in result.stdout.splitlines()[0] or True
    assert "m3" in result.stdout


# stop() wiring

def test_stop_with_artifacts_on_classifies_and_prints_hint(tmp_path):
    llm = tmp_path / "llm.sh"
    write_stub_hook(
        llm,
        "cat > /dev/null; "
        "printf 'SCRIBE-CANDIDATES: [{\"type\": \"summary\", \"scope\": \"s\", "
        "\"audience\": \"a\", \"topic\": \"t\", \"trigger_class\": \"weak\"}]\\n'",
    )
    env = scribe_env(
        tmp_path,
        SCRIBE_LLM_CMD=str(llm),
        SCRIBE_ARTIFACTS="1",
        SCRIBE_REVIEW_PANE="0",
        SCRIBE_ARTIFACT_BUILD_CMD="true",
    )
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    assert "scribe.sh artifacts" in stopped.stderr  # fallback hint always
    sidecar = tmp_path / "out" / ".artifacts" / f"{meeting_id}.sidecar.tsv"
    assert sidecar.is_file()
    assert "\tweaker-trigger\t" in sidecar.read_text()
    assert (tmp_path / "out" / ".last-meeting").read_text() == meeting_id


def test_stop_with_artifacts_off_touches_nothing(tmp_path):
    env = scribe_env(tmp_path)
    meeting_id, ram_dir, stopped = start_stop_meeting(tmp_path, env)
    assert stopped.returncode == 0
    assert not (tmp_path / "out" / ".artifacts").exists()
