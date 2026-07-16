import os
import subprocess
import pathlib
import tempfile
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
