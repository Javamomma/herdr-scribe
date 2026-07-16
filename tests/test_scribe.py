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
