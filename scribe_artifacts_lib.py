"""scribe_artifacts_lib — shared plumbing for the auto-artifacts system (§4).

ZERO MODEL CALLS LIVE HERE. This module (and the `scribe-artifacts` review
CLI built on it) is deterministic: sidecar/run-log/task-file IO, meeting
resolution, rendering, and build *dispatch* (spawning the separate
`scribe-artifacts-build` process, which is where model invocation lives).
A test scans these sources to keep it that way.

Files and schemas
-----------------
Candidate sidecar   <artifacts-dir>/<meeting>.sidecar.tsv
    id  disposition  type  scope  audience  topic  trigger_class  note_path
    One row per classifier candidate — built and skipped alike; the sidecar
    is what makes a skipped candidate approvable later. Ephemeral working
    state (git-ignored); the durable record is the run log.

Run log (append-only, durable)   <artifacts-dir>/run.log
    ts  event  note_path  type  topic  artifact_path  detail
    event ∈ {built, build-failed}.

Review tasks   <artifacts-dir>/review-tasks.tsv
    artifact_path  status  ts        status ∈ {open, done}

Every field is sanitized against the FULL line-break class before writing —
everything str.splitlines() splits on, plus the field separator — because a
single stray \r in a free-text topic would silently split one row into two
malformed ones, corrupting exactly the record this file exists to protect.
"""

import os
import shutil
import subprocess
import sys
import tempfile

# Closed disposition vocabulary — defined once, reused everywhere.
DISPOSITIONS = ("built", "approved", "over-cap", "weaker-trigger", "disabled-type")
QUEUED_DISPOSITIONS = ("over-cap", "weaker-trigger")
BUILT_DISPOSITIONS = ("built", "approved")

RUN_LOG_EVENTS = ("built", "build-failed")

# The exact text render_view shows for a build that was dispatched but has
# not landed in the run log yet. Defined once: the review pane keys its
# auto-refresh off this marker appearing in the rendered view (§4h).
BUILD_PENDING_LABEL = "building…"

SIDECAR_COLUMNS = 8

# Everything Python's str.splitlines() splits on (minus \r\n which is covered
# per-char), plus the TSV field separator. NOT just \t and \n.
_SEPARATOR_CHARS = "\t\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


class ArtifactsError(Exception):
    """Operator-facing error; the CLI prints it to stderr and exits non-zero."""


class UnknownIdError(ArtifactsError):
    pass


class DuplicateIdError(ArtifactsError):
    pass


class AmbiguousMeetingError(ArtifactsError):
    pass


class UnknownMeetingError(ArtifactsError):
    """An EXPLICITLY named meeting matches nothing on record — no sidecar
    and no run-log trace. The CLI maps this to its own exit code: silently
    falling through to the §4i log-derived view would render some OTHER
    meeting's builds under the operator's typo."""


def sanitize_field(value):
    """Make a value safe to embed as one TSV field: every character our
    reader would treat as a field or record separator becomes a space."""
    value = "" if value is None else str(value)
    for ch in _SEPARATOR_CHARS:
        value = value.replace(ch, " ")
    return value


def normalize_key(value):
    """Normalize once, use everywhere (§6.3): the same normalization is
    applied when a key field is WRITTEN (run log, sidecar) and when it is
    COMPARED (joins), so a stray trailing space can never make a lookup miss."""
    return sanitize_field(value).strip()


def utc_now():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- configuration -------------------------------------------------------

def out_dir():
    return os.environ.get("SCRIBE_OUTPUT_DIR") or os.path.join(os.getcwd(), "meetings")


def artifacts_dir():
    return os.environ.get("SCRIBE_ARTIFACTS_DIR") or os.path.join(out_dir(), ".artifacts")


def run_log_path():
    return os.environ.get("SCRIBE_RUN_LOG") or os.path.join(artifacts_dir(), "run.log")


def review_tasks_path():
    return os.environ.get("SCRIBE_REVIEW_TASKS") or os.path.join(
        artifacts_dir(), "review-tasks.tsv"
    )


def artifact_out_dir():
    return os.environ.get("SCRIBE_ARTIFACT_OUT_DIR") or os.path.join(out_dir(), "artifacts")


def sidecar_path(meeting):
    return os.path.join(artifacts_dir(), f"{meeting}.sidecar.tsv")


def parse_cap():
    """SCRIBE_ARTIFACT_CAP: default 6; '0' legitimately means 'queue
    everything, build nothing'. An inline comment is stripped before
    validation (`2  # keep it low` → 2). A malformed value falls back to the
    default WITH a stderr warning — but note the direction: the default may
    be HIGHER than what the operator meant, so silence here would quietly run
    more unattended builds than they asked for."""
    raw = os.environ.get("SCRIBE_ARTIFACT_CAP", "")
    if raw == "":
        return 6
    value = raw.split("#", 1)[0].strip()
    if value.isdigit():
        return int(value)
    print(
        f"scribe-artifacts: warning: SCRIBE_ARTIFACT_CAP={raw!r} is not a "
        f"non-negative integer; using the default (6)",
        file=sys.stderr,
    )
    return 6


def pane_refresh_seconds():
    """SCRIBE_ARTIFACTS_PANE_REFRESH: how many seconds the review pane's
    prompt waits before re-rendering, while the view still shows a build
    that has not landed (§4h). Default 15; fractional values are fine.
    '0' disables auto-refresh entirely — the prompt blocks exactly as it
    did before the refresh existed. A malformed or negative value falls
    back to the default WITH a stderr warning (same convention as
    parse_cap): the operator asked for *some* refresh cadence, so silently
    losing it is the wrong direction."""
    raw = os.environ.get("SCRIBE_ARTIFACTS_PANE_REFRESH", "")
    if raw.strip() == "":
        return 15.0
    try:
        value = float(raw)
    except ValueError:
        value = None
    # NaN fails the >= 0 comparison; infinity would be a nonsense timeout.
    if value is not None and value >= 0 and value != float("inf"):
        return value
    print(
        f"scribe-artifacts: warning: SCRIBE_ARTIFACTS_PANE_REFRESH={raw!r} "
        f"is not a non-negative number of seconds; using the default (15)",
        file=sys.stderr,
    )
    return 15.0


def enabled_types():
    """SCRIBE_ARTIFACT_TYPES: comma/space-separated allowlist of artifact
    types. Unset/empty → every type is enabled (the generic generator can
    draft any of them); set → anything else is disposition disabled-type."""
    raw = os.environ.get("SCRIBE_ARTIFACT_TYPES", "").strip()
    if not raw:
        return None
    return {t for t in raw.replace(",", " ").split() if t}


def slugify(value):
    out = []
    for ch in str(value).lower():
        out.append(ch if (ch.isalnum()) else "-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "untitled"


# --- candidate model -----------------------------------------------------

class Candidate:
    __slots__ = ("id", "disposition", "type", "scope", "audience", "topic",
                 "trigger_class", "note_path")

    def __init__(self, id, disposition, type, scope, audience, topic,
                 trigger_class, note_path):
        self.id = str(id)
        self.disposition = disposition
        self.type = type
        self.scope = scope
        self.audience = audience
        self.topic = topic
        self.trigger_class = trigger_class
        self.note_path = note_path

    def to_row(self):
        return "\t".join(
            sanitize_field(v)
            for v in (
                self.id, self.disposition, self.type, self.scope,
                self.audience, self.topic, self.trigger_class, self.note_path,
            )
        )

    @classmethod
    def from_fields(cls, fields):
        """Tolerant: pad short rows, drop extra columns. Correct for READING;
        never write a sidecar back from this lossy form (§4g)."""
        padded = list(fields[:SIDECAR_COLUMNS]) + [""] * (SIDECAR_COLUMNS - len(fields))
        return cls(*padded)


# --- sidecar IO ----------------------------------------------------------

def load_sidecar(meeting):
    """Return (state, candidates) where state ∈ {'present', 'absent',
    'unreadable'}.

    Absent-vs-empty is load-bearing (§4i): only a genuinely ABSENT file may
    trigger log-derived reconstruction — a meeting whose classifier proposed
    nothing legitimately writes a zero-row sidecar and must render as such.
    'Present but unreadable' (a directory at the path, permissions, decode
    errors, a broken symlink — something is known to exist there) also goes
    to the present branch, reported, never silently reconstructed."""
    path = sidecar_path(meeting)
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            raw = f.read()
    except FileNotFoundError:
        # A broken symlink raises FileNotFoundError too, but something IS
        # at that path — deliberate §6.6 choice: treat it as unreadable.
        if os.path.lexists(path):
            return "unreadable", []
        return "absent", []
    except (OSError, UnicodeDecodeError):
        return "unreadable", []
    candidates = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        candidates.append(Candidate.from_fields(line.split("\t")))
    return "present", candidates


def write_sidecar(meeting, candidates):
    """Write the full sidecar (classification time — the single writer).
    Failure warns on stderr and returns False; it must NEVER take down
    classification (§4c)."""
    path = sidecar_path(meeting)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=".sidecar-tmp-"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
                for cand in candidates:
                    f.write(cand.to_row() + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except OSError as exc:
        print(
            f"scribe-artifacts: warning: could not write sidecar {path}: {exc} "
            f"— skipped candidates for this meeting will NOT be approvable",
            file=sys.stderr,
        )
        return False


def edit_sidecar_disposition(meeting, target_id, new_disposition):
    """Targeted RAW-TEXT edit (§4g): find the one line whose first field is
    the target id, replace only its disposition field, and write every other
    line back byte-for-byte — short rows, over-long rows, and blank lines
    included. Never round-trip through the tolerant parsed form: a parser
    that pads and truncates is catastrophic as a writer.

    Atomic: temp file in the same directory, flush+fsync, original mode
    copied onto the temp file, rename over the target."""
    if new_disposition not in DISPOSITIONS:
        raise ArtifactsError(
            f"invalid disposition {new_disposition!r} (valid: {', '.join(DISPOSITIONS)})"
        )
    path = sidecar_path(meeting)
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            raw = f.read()
    except OSError as exc:
        raise ArtifactsError(f"cannot read sidecar {path}: {exc}")

    lines = raw.splitlines(keepends=True)
    matches = []
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        first = body.split("\t", 1)[0]
        if first == str(target_id):
            matches.append(i)
    if not matches:
        raise UnknownIdError(f"no candidate with id {target_id} in {path}")
    if len(matches) > 1:
        raise DuplicateIdError(
            f"duplicate id {target_id} in {path} — sidecar is corrupt; refusing to guess"
        )

    i = matches[0]
    line = lines[i]
    terminator = line[len(line.rstrip("\r\n")):]
    fields = line.rstrip("\r\n").split("\t")
    while len(fields) < 2:
        fields.append("")
    fields[1] = sanitize_field(new_disposition)
    lines[i] = "\t".join(fields) + terminator

    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".sidecar-tmp-")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
            f.flush()
            os.fsync(f.fileno())
        shutil.copystat(path, tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --- run log -------------------------------------------------------------

def append_run_log(event, note_path, type_, topic, artifact_path="", detail=""):
    if event not in RUN_LOG_EVENTS:
        raise ArtifactsError(f"invalid run-log event {event!r}")
    path = run_log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = "\t".join(
        [
            utc_now(),
            sanitize_field(event),
            normalize_key(note_path),
            normalize_key(type_),
            normalize_key(topic),
            sanitize_field(artifact_path),
            sanitize_field(detail),
        ]
    )
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(line + "\n")


def read_run_log():
    path = run_log_path()
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            raw = f.read()
    except OSError:
        return []
    entries = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        padded = list(fields[:7]) + [""] * (7 - len(fields))
        entries.append(
            {
                "ts": padded[0],
                "event": padded[1],
                "note_path": padded[2],
                "type": padded[3],
                "topic": padded[4],
                "artifact_path": padded[5],
                "detail": padded[6],
            }
        )
    return entries


def last_outcome(note_path, type_, topic):
    """Most recent run-log outcome for a candidate. The join key is
    (note_path, type, topic) — THREE fields (§4e): recurring meetings
    legitimately repeat a topic, and a two-field key would let one week's
    BUILT line satisfy every week's row. Reverse scan so a retried build's
    latest outcome wins."""
    key = (normalize_key(note_path), normalize_key(type_), normalize_key(topic))
    for entry in reversed(read_run_log()):
        if (entry["note_path"], entry["type"], entry["topic"]) == key:
            return entry
    return None


# --- review tasks --------------------------------------------------------

def append_review_task(artifact_path, status="open"):
    path = review_tasks_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = "\t".join([sanitize_field(artifact_path), sanitize_field(status), utc_now()])
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(line + "\n")


def task_status(artifact_path):
    """Review-task state for an artifact. The path is matched as a full
    delimited field, never a substring (docs/out.md must not match
    archive/docs/out.md), and if several rows reference the same path,
    ANY open row wins — reporting 'done' while an open row exists is the
    unsafe direction (§4e)."""
    path = review_tasks_path()
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            raw = f.read()
    except OSError:
        return None
    target = sanitize_field(artifact_path)
    saw_any = False
    saw_open = False
    for line in raw.splitlines():
        fields = line.split("\t")
        if not fields or fields[0] != target:
            continue
        saw_any = True
        status = fields[1] if len(fields) > 1 else ""
        if status != "done":
            saw_open = True
    if not saw_any:
        return None
    return "needs review" if saw_open else "reviewed"


# --- meeting resolution --------------------------------------------------

def known_meetings():
    d = artifacts_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    suffix = ".sidecar.tsv"
    return sorted(n[: -len(suffix)] for n in names if n.endswith(suffix))


def last_meeting_id():
    try:
        with open(os.path.join(out_dir(), ".last-meeting"), "r", encoding="utf-8") as f:
            value = f.read().strip()
        return value or None
    except OSError:
        return None


def run_log_meetings():
    """Meeting ids the run log knows about, derived from its note paths
    (`stop` classifies `<out-dir>/<meeting>.note.md`, so the basename carries
    the id). This is what lets an explicit id for a pre-sidecar meeting keep
    reaching the §4i reconstruction path after resolve_meeting started
    rejecting ids that match nothing at all."""
    suffix = ".note.md"
    ids = set()
    for entry in read_run_log():
        base = os.path.basename(entry["note_path"])
        if base.endswith(suffix) and len(base) > len(suffix):
            ids.add(base[: -len(suffix)])
    return ids


def resolve_meeting(query):
    """Resolve a meeting reference to an id. Ambiguity is an ERROR that
    names every match — never a silent pick-the-newest (§6.4).

    An EXPLICIT query that matches no sidecar meeting resolves to itself
    ONLY while something on record still knows it — a sidecar file at its
    exact path, or a run-log note (see run_log_meetings) — so the §4i
    no-record/reconstruction paths keep working for genuinely pre-sidecar
    meetings. A query nothing knows raises UnknownMeetingError instead:
    left to resolve to itself, the reconstruction fallback would render the
    run log's latest builds under the operator's typo rather than saying
    the id matched nothing. The default/latest path (query None) never
    raises this — reconstruction is exactly what it exists for."""
    meetings = known_meetings()
    if query is None:
        last = last_meeting_id()
        if last:
            return last
        if meetings:
            return meetings[-1]
        raise ArtifactsError("no meetings on record and none specified")
    if query in meetings:
        return query
    matches = [m for m in meetings if query in m]
    if len(matches) > 1:
        raise AmbiguousMeetingError(
            f"'{query}' matches more than one meeting: {', '.join(matches)} — be specific"
        )
    if matches:
        return matches[0]
    # os.path.lexists covers a sidecar known_meetings could not list (an
    # unreadable artifacts dir); substring matching against run-log ids
    # mirrors the sidecar matching above, but the query is returned verbatim
    # either way — reconstruction picks and names its own source group.
    if os.path.lexists(sidecar_path(query)):
        return query
    logged = run_log_meetings()
    if query in logged or any(query in m for m in logged):
        return query
    recorded = sorted(set(meetings) | logged)
    if recorded:
        raise UnknownMeetingError(
            f"meeting '{query}' matches nothing on record — no sidecar and "
            f"no run-log entry knows it. Meetings with records: "
            f"{', '.join(recorded)}"
        )
    raise UnknownMeetingError(
        f"meeting '{query}' matches nothing on record — no sidecar and no "
        f"run-log entry knows it (no meetings have records here yet)"
    )


# --- build dispatch (NO model call happens in this process) --------------

def dispatch_build(meeting, candidate):
    """Spawn the detached builder for one candidate. The build itself (and
    its model invocation) lives in scribe-artifacts-build, a separate
    process; SCRIBE_ARTIFACT_BUILD_CMD is a test seam that replaces it and
    receives meeting/id/note/type/scope/audience/topic as $1..$7."""
    seam = os.environ.get("SCRIBE_ARTIFACT_BUILD_CMD", "")
    if seam:
        argv = [
            "bash", "-c", seam, "_",
            meeting, candidate.id, candidate.note_path, candidate.type,
            candidate.scope, candidate.audience, candidate.topic,
        ]
    else:
        builder = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "scribe-artifacts-build")
        argv = [
            sys.executable, builder, "build",
            "--meeting", meeting,
            "--id", candidate.id,
            "--note", candidate.note_path,
            "--type", candidate.type,
            "--scope", candidate.scope,
            "--audience", candidate.audience,
            "--topic", candidate.topic,
        ]
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- approval ------------------------------------------------------------

def approve(meeting, ids, out=sys.stdout, err=sys.stderr):
    """Approve queued candidates. Contract (§4f):
      - resolve EVERY requested id first; one bad id → zero builds, errors
        on stderr, nothing on stdout, distinct exit (raised here);
      - reconstructed L-ids are rejected with an explanation;
      - record the approval (sidecar edit) BEFORE dispatching — the local
        write is cheap and reversible, the detached build is neither;
      - a per-id failure mid-batch warns, skips, CONTINUES, and the batch
        exits non-zero at the end;
      - a candidate whose note no longer resolves is refused before its
        approval is recorded (the row stays queued)."""
    state, candidates = load_sidecar(meeting)
    if state == "absent":
        raise ArtifactsError(
            f"no sidecar for meeting '{meeting}' — its review view is "
            f"log-derived and cannot be approved (the log kept no "
            f"scope/audience to rebuild from)"
        )
    if state == "unreadable":
        raise ArtifactsError(f"sidecar for meeting '{meeting}' exists but cannot be read")

    by_id = {}
    for cand in candidates:
        by_id.setdefault(cand.id, []).append(cand)

    queued_ids = [c.id for c in candidates if c.disposition in QUEUED_DISPOSITIONS]

    # Resolve everything first; dispatch nothing until all resolve.
    resolved = []
    for raw_id in ids:
        raw_id = str(raw_id)
        if raw_id.startswith("L"):
            raise ArtifactsError(
                f"id {raw_id} belongs to a log-derived, reconstructed view and "
                f"cannot be approved (no scope/audience was recorded to rebuild "
                f"from). Approvable ids: {', '.join(queued_ids) or '(none)'}"
            )
        rows = by_id.get(raw_id, [])
        if not rows:
            raise UnknownIdError(
                f"unknown id {raw_id}. Approvable ids: {', '.join(queued_ids) or '(none)'}"
            )
        if len(rows) > 1:
            raise DuplicateIdError(
                f"duplicate id {raw_id} in sidecar — refusing to guess which to approve"
            )
        cand = rows[0]
        if cand.disposition not in QUEUED_DISPOSITIONS:
            raise ArtifactsError(
                f"id {raw_id} is not queued (disposition: {cand.disposition}). "
                f"Approvable ids: {', '.join(queued_ids) or '(none)'}"
            )
        resolved.append(cand)

    failures = 0
    for cand in resolved:
        # Refuse to dispatch when the source note does not resolve — BEFORE
        # recording the approval (§4d). Otherwise the generator writes an
        # orphan draft nothing references and the row sticks at 'building'.
        if not cand.note_path or not os.path.isfile(cand.note_path):
            print(
                f"scribe-artifacts: id {cand.id}: source note not found at "
                f"'{cand.note_path or '(empty)'}' — not building; row left queued",
                file=err,
            )
            failures += 1
            continue
        try:
            edit_sidecar_disposition(meeting, cand.id, "approved")
        except ArtifactsError as exc:
            print(f"scribe-artifacts: id {cand.id}: could not record approval: {exc}",
                  file=err)
            failures += 1
            continue
        try:
            dispatch_build(meeting, cand)
        except OSError as exc:
            print(f"scribe-artifacts: id {cand.id}: build dispatch failed: {exc}",
                  file=err)
            failures += 1
            continue
        print(f"approved {cand.id}: {cand.type} — {cand.topic} (building)", file=out)
    return failures


# --- rendering -----------------------------------------------------------

def _built_row_status(cand):
    outcome = last_outcome(cand.note_path, cand.type, cand.topic)
    if outcome is None:
        return BUILD_PENDING_LABEL, ""
    if outcome["event"] == "build-failed":
        return f"FAILED: {outcome['detail'] or 'see builder logs'}", ""
    review = task_status(outcome["artifact_path"]) or "no review task"
    return outcome["artifact_path"], review


def render_view(meeting, out=sys.stdout):
    state, candidates = load_sidecar(meeting)
    if state == "absent":
        return _render_reconstructed(meeting, out)

    print(f"Artifacts for meeting {meeting}", file=out)
    print(f"Source: sidecar {sidecar_path(meeting)}", file=out)
    if state == "unreadable":
        print(
            "WARNING: the sidecar exists but cannot be read — fix it before "
            "trusting or approving anything below.",
            file=out,
        )
        print(file=out)

    built = [c for c in candidates if c.disposition in BUILT_DISPOSITIONS]
    queued = [c for c in candidates if c.disposition in QUEUED_DISPOSITIONS]
    disabled = [c for c in candidates if c.disposition == "disabled-type"]
    other = [
        c
        for c in candidates
        if c.disposition not in BUILT_DISPOSITIONS + QUEUED_DISPOSITIONS + ("disabled-type",)
    ]

    # Every group always prints, even at zero — a stable shape (§4e).
    print(file=out)
    print(f"BUILT ({len(built)}):", file=out)
    for c in built:
        where, review = _built_row_status(c)
        suffix = f" · {review}" if review else ""
        print(f"  [{c.id}] {c.type} — {c.topic} → {where}{suffix}", file=out)

    print(f"QUEUED ({len(queued)}):", file=out)
    for c in queued:
        print(f"  [{c.id}] {c.type} — {c.topic} ({c.disposition})", file=out)

    print(f"disabled-type ({len(disabled)}):", file=out)
    for c in disabled:
        print(f"  [{c.id}] {c.type} — {c.topic} (type not enabled; not approvable)", file=out)

    # Catch-all so nothing can vanish from the accounting.
    print(f"OTHER ({len(other)}):", file=out)
    for c in other:
        print(f"  [{c.id}] {c.type} — {c.topic} (disposition: {c.disposition!r})", file=out)

    print(file=out)
    if queued:
        hint_ids = " ".join(c.id for c in queued)
        print(f"Approve with: scribe.sh artifacts {meeting} --approve {hint_ids}", file=out)
    else:
        print("Nothing queued for approval.", file=out)
    return 0


def _render_reconstructed(meeting, out):
    """§4i log-derived fallback for meetings that predate the sidecar. The
    view says what it is, names its single source group in the banner AND on
    each row, synthesizes visibly different L-ids, and never lets the reader
    infer that nothing was skipped."""
    entries = [e for e in read_run_log() if e["event"] in RUN_LOG_EVENTS]
    print(f"Artifacts for meeting {meeting}", file=out)
    if not entries:
        print(
            f"No record for '{meeting}': no sidecar and no run-log entries. "
            f"(A typo'd meeting id looks the same as a pre-sidecar meeting.)",
            file=out,
        )
        return 0
    source_note = entries[-1]["note_path"]
    rows = [e for e in entries if e["note_path"] == source_note]
    print(
        f"Source: RECONSTRUCTED from the run log — this meeting has no "
        f"candidate sidecar (it may predate the sidecar, or the id may be "
        f"mistyped). Showing builds recorded for note: {source_note}",
        file=out,
    )
    print(
        "NOTE: skipped candidates CANNOT be recovered or approved for this "
        "view — the log kept a topic but not the scope/audience needed to "
        "rebuild. Do not read this as 'nothing was skipped'.",
        file=out,
    )
    print(file=out)
    print(f"BUILT ({len(rows)}) [log-derived]:", file=out)
    for i, e in enumerate(rows, 1):
        label = os.path.basename(e["note_path"]) or e["note_path"]
        where = e["artifact_path"] if e["event"] == "built" else f"FAILED: {e['detail']}"
        print(f"  [L{i}] {e['type']} — {e['topic']} → {where}  (from {label})", file=out)
    print(file=out)
    print("Nothing here is approvable (L-ids are reconstructed).", file=out)
    return 0


def render_all(out=sys.stdout):
    """Bounded cross-meeting summary — and the bound is stated (§6.5)."""
    raw_limit = os.environ.get("SCRIBE_ARTIFACTS_ALL_LIMIT", "20")
    limit = int(raw_limit) if raw_limit.isdigit() and int(raw_limit) > 0 else 20
    meetings = known_meetings()
    shown = meetings[-limit:]
    dropped = len(meetings) - len(shown)
    print(f"Artifact summary — showing {len(shown)} of {len(meetings)} meetings "
          f"(limit {limit}; oldest {dropped} not shown)", file=out)
    for meeting in shown:
        state, candidates = load_sidecar(meeting)
        if state != "present":
            print(f"  {meeting}: sidecar {state}", file=out)
            continue
        built = sum(1 for c in candidates if c.disposition in BUILT_DISPOSITIONS)
        queued = sum(1 for c in candidates if c.disposition in QUEUED_DISPOSITIONS)
        rest = len(candidates) - built - queued
        print(f"  {meeting}: {built} built/approved · {queued} queued · {rest} other",
              file=out)
    return 0
