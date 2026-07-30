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


def test_manifest_has_two_panes():
    """The manifest defines the transcript pane and the analyst pane."""
    data = load_manifest()
    assert "panes" in data
    assert len(data["panes"]) == 2
    pane_ids = {p["id"] for p in data["panes"]}
    assert pane_ids == {"transcript", "analyst"}


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
