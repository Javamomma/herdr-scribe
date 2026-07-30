# herdr-scribe — Parity Port Brief

**For the implementing agent.** This brief brings `herdr-scribe` up to the
capability level of a mature private downstream deployment of the same design,
*without* importing any domain-specific behavior.

Read `docs/DESIGN.md`, `docs/EXTRACTION-BRIEF.md`, and
`scripts/sanitization-gate.sh` first. Everything in the extraction brief still
binds — this document only adds to it. In particular:

- **Clean-room.** Build from these descriptions. Copy nothing verbatim from any
  other codebase. This brief deliberately contains specifications, contracts,
  failure modes, and test cases — not source to paste.
- **No recording, ever.** Audio exists only in an in-memory pipe. Nothing here
  changes that, and several additions below are constrained by it.
- **`SCRIBE_ON_STOP` remains the only downstream extension point.**
- **The sanitization gate must pass** (`bash scripts/sanitization-gate.sh`,
  exit 0, zero HARD hits) before anything ships. This file was written to pass
  it; keep it that way as you edit.
- **Model-agnostic.** Everything that calls a model goes through a configurable
  command. No provider or model id is hardcoded.

---

## 0. The one architectural decision that governs everything below

The extraction brief lists confidentiality, preservation-hold, record-lifecycle,
domain-taxonomy, and document-generation behavior as **non-goals** of this repo,
belonging to "an optional private downstream layer that attaches via
`SCRIBE_ON_STOP`."

That boundary is correct and this brief keeps it. But the private deployment
proved that **most of the capability people actually want is domain-neutral** —
it only *looked* domain-specific because it grew up inside one domain. So each
item below is explicitly assigned:

| Bucket | Rule |
|---|---|
| **CORE** | Domain-neutral mechanism. Build it here. |
| **SEAM** | A hook/contract in core that a downstream layer plugs policy into. Core ships the seam and a neutral default; core never ships the policy. |
| **OUT** | Stays downstream. Do not build it here, and do not let its vocabulary leak into core. |

If you find yourself writing a domain noun into core, you have mis-assigned
something. Stop and re-read §4.

**Vocabulary discipline.** The private deployment names its work units with a
domain noun. Core must use a neutral one. This brief uses **`scope`** for "the
work item a meeting or artifact belongs to" and **`artifact`** for "a document a
meeting implies should be written." Use those, or pick your own neutral pair and
use it consistently — but never a domain-specific noun.

---

## 1. CORE — Two-tier analyst with document retrieval

**Today:** one loop, one model, transcript(+optional OCR) in, rolling brief out.

**The gap:** the single most valuable thing the mature version does is answer
*"what does the document we are arguing about actually say?"* mid-meeting, with
verbatim text and a source path. A summarizer cannot do that; it needs a
retrieval pass over a corpus, and that pass is too slow and too expensive to run
every interval.

**Build:** split the analyst into two tiers.

### 1a. Light tier (every interval — the existing loop)

Keep the current cadence and cost profile. Extend its prompt contract so the
model emits, in addition to the human-readable brief, **one machine-readable
trigger line** when the conversation references a document, section, clause,
citation, or identifier that a reader would need the actual text of:

```
RETRIEVE: <short retrieval query>
```

Rules:
- Exactly one such line at most per cycle. Absent when nothing is referenced.
- The trigger line is **stripped from the pane output** — it is control data,
  not something a human should read.
- Parse it with an anchored pattern and treat an absent/garbled line as "no
  retrieval this cycle." Never let a malformed trigger break the brief.

### 1b. Deep tier (on trigger only — new)

A separate, detached worker that, given the retrieval query:

1. Searches a configured corpus **read-only**. Grant it read/search tools only.
   It must not be able to write, and must not be able to reach the network
   beyond whatever the model command itself needs.
2. Extracts the relevant passage and appends a clearly delimited block to the
   same brief file: a short label, the **verbatim** quoted passage, and the
   **source path**. Verbatim and path-cited are the whole point — a paraphrase
   is worse than nothing, because the reader will rely on it live.
3. If it cannot find the passage, it says so explicitly. It must never
   fabricate, approximate, or "reconstruct" text. State this as a hard
   instruction in the prompt.

Non-obvious requirements, each learned from production:

- **Single-in-flight.** Use an atomic `mkdir` as the lock (not a file existence
  test, which races). If a lock is held when a new trigger arrives, write the
  query to a single `pending` slot — *coalescing*, not queueing. One stale
  answer is better than a backlog of them.
- **Drain pending at the top of each light cycle**, only when the lock is free.
- **Time-boxed.** Wrap the deep call in a hard timeout. A hung retrieval must
  never wedge the analyst loop or the meeting.
- **Serialize the append.** Both tiers append to the same brief file from
  different processes. Use an advisory file lock around the append or you will
  interleave two blocks mid-line.
- **RAM-only.** Any scratch the deep tier extracts lives in the meeting's
  in-memory directory and dies with it. Never write extracted document text to
  durable storage — the operator did not ask you to copy their corpus.
- **Toggle + config:** an off switch, a separate model command, a timeout, and
  the corpus root. Deep defaults **off** if no corpus root is configured, so a
  fresh install behaves exactly as today.

**Config to add:** deep enable/disable, `SCRIBE_LLM_CMD_DEEP` (falls back to
`SCRIBE_LLM_CMD`), deep timeout seconds, corpus root path(s), and a document
text-extraction command for non-plaintext files (see §2).

**Tests:** trigger parsed / absent / malformed; trigger line stripped from pane
output; lock prevents a second concurrent launch; pending query coalesces to one;
timeout kills a hung deep call and the loop survives; concurrent appends from
both tiers do not interleave (see §6.1 for how to write this test without
deadlocking it).

---

## 2. CORE — Document text extraction helper

The deep tier is worthless if the corpus is mostly binary formats. Add a small
`scribe-doc2text`-style helper: given a path, print plain text to stdout;
dispatch on extension to whatever extractors are present on the host; exit
non-zero with a clear message when no extractor is available.

Keep it dependency-optional and degrade loudly, never silently: a corpus file
that cannot be read must produce "could not extract <path>", not an empty string
that the model then interprets as "the document is blank."

**Tests:** plaintext passthrough; unknown extension → non-zero + message;
missing extractor → non-zero + message; extractor command overridable by env so
tests need no real binaries.

---

## 3. CORE — Per-meeting glossary injection

**Today:** one static glossary file of hotwords.

**The gap:** the terms worth biasing toward are per-meeting — the identifiers,
party names, and jargon specific to *this* conversation. A single global list either
goes stale or grows until it biases everything.

**Build:** at `start`, when a scope is supplied, derive hotwords from that
scope's own context and pass them **inline** to the recognizer for this meeting
only. The reviewed global glossary file is **never mutated** — that file is a
human-curated artifact and an automated writer will wreck it.

Precedence: inline per-meeting terms are additive on top of the global file.

**Tests:** inline terms reach the recognizer; the global file is byte-unchanged
after a start that injected terms; no scope supplied → global file only;
duplicate terms de-duplicated.

---

## 4. CORE — Auto-artifacts, with human review and sign-off

This is the largest addition and the one with the most hard-won detail. It is
also **entirely domain-neutral**: "the meeting implied a document should be
written; draft it and let me review it" applies to a standup, a design review,
or a client call equally.

### 4a. Classification

After the note is written (and after any stop-gate clears — §5), run a
**classifier** over the **note, never the transcript**. This matters: the note is
the reviewed, structured artifact; the transcript is raw and may contain
misrecognitions. Deriving documents from the transcript propagates ASR errors
into deliverables.

The classifier emits a machine-readable list of candidates, each with:

| Field | Purpose |
|---|---|
| `type` | Which artifact generator to run (from a configured enabled set) |
| `scope` | The work item it belongs to |
| `audience` | Who it is for — drives generator behavior downstream |
| `topic` | Short human label; also the identity key |
| `trigger_class` | How strongly the note implies it: explicit / confident inference / weaker |

Emit it as **one single-line machine payload** (e.g. one JSON line behind a
sentinel prefix) and parse defensively: not-a-list, list-of-non-objects, missing
keys, and no payload at all must all degrade to "no candidates," never a crash.

### 4b. The cap — and why it must be configurable

Auto-building is a real cost: each build spawns a model run that writes a
document. Cap the number built automatically per meeting.

- Default **6**. Make it configurable via the config file **and** an env var.
- The env var wins; then the config file; then the default.
- **Validate conservatively.** A malformed cap value must fall back to the
  default — and note which direction "safe" runs: silently *raising* the cap
  runs more unattended model builds than the operator asked for. Treat an
  unparseable value as a reason to be quiet, not permissive.
- `0` is a legitimate value meaning "queue everything, build nothing."
- **Strip inline comments before validating.** `cap = 2  # keep it low` must
  yield `2`. Getting this wrong silently reverts the operator to the default.
- If your config file is a bare-word list *and* now carries `key = value` lines,
  the two readers must be **disjoint**: the list reader must skip lines
  containing `=`, and the key reader must ignore bare words. Otherwise `cap = 6`
  registers as three list entries.

**A skipped candidate must never consume cap budget.** Increment the built
counter only on an actual build.

### 4c. The candidate sidecar — the keystone

**The problem this solves:** if you only log "skipped: <topic> (over cap)", you
have thrown away the `scope` and `audience` needed to build it later. The
operator can see that something was skipped but can never act on it. That makes
the cap a silent data-loss mechanism.

**Build:** for every meeting, persist **every** candidate — built and skipped
alike — to a per-meeting **sidecar** file: one tab-separated row per candidate.

```
id  disposition  type  scope  audience  topic  trigger_class  note_path
```

- `id` — stable, 1-based, assigned in classifier order, to **all** candidates
  including ones skipped as a disabled type. Assign the id *before* the
  disposition branch, or a future edit will silently renumber.
- `disposition` — a small closed vocabulary: built / approved / over-cap /
  weaker-trigger / disabled-type. Define it once as a constant and reuse it.
- `note_path` — the source note. **This column exists so an approved candidate
  builds from its own meeting's note.** Never recover a note path by searching a
  shared log; the most recent entry in a shared log belongs to whichever meeting
  ran last, so a search will attach the wrong meeting's note to an older
  candidate and generate a document from the wrong source.
- **Sanitize every field** against the field and record separators before
  writing. Not just tab and `\n` — the *entire* set your reader treats as a line
  break. In Python that is everything `str.splitlines()` splits on
  (`\r`, `\v`, `\f`, `\x1c`–`\x1e`, `\x85`, and two Unicode separators), not just
  `\n`. A single stray `\r` in a free-text topic silently splits one row into two
  malformed ones — corrupting exactly the record this file exists to protect.
- Treat the sidecar as **ephemeral working state** (git-ignored). The durable
  record stays the append-only run log.

**The sidecar must never take down classification.** It is an enablement
artifact. If its directory cannot be created or the file cannot be written:
warn on **stderr** and continue — the classification output must still be
produced in full. Note the shell trap here: under `set -e`, a `mkdir -p` that is
the last command of a top-level `&&` list aborts the enclosing shell *before* any
output is emitted, and skips `trap ... RETURN` cleanup. Guard it explicitly.

Degrade **loudly**. A silently missing sidecar means approval silently
unavailable, and the operator has no way to know.

### 4d. Building

For each candidate within cap, dispatch a **detached** build. The build:

- Runs the generator for that `type`, drawing on the note plus the scope's
  history.
- Is **idempotent** — check whether this `(note, type, topic)` was already built
  and skip if so. Approval retries and double-clicks are normal.
- Writes the draft to that generator's normal output location.
- On success: append a run-log line, back-link the draft into the note, and
  create a review task/row so an unreviewed draft cannot go unnoticed.
- **Never transmits anything.** No mail, no upload, no post. Say this in the
  generator prompt as well as enforcing it in code. The output is a draft for a
  human to review.

**Refuse to dispatch when the source note does not resolve.** Check *before*
recording the approval. If the note path is empty or no longer exists (moved,
renamed, archived between the meeting and the approval), print the id and the
path you looked for, mark the batch as having a casualty, and **do not build**.
Otherwise the generator writes a draft and then the back-link step fails, leaving
a document on disk that nothing references, no log line, no review row, and a
status stuck at "building" forever — the exact failure the review surface exists
to prevent.

### 4e. The review surface — `artifacts` subcommand

A deterministic, **zero-model** reader/approver. This costs no tokens and must
never call a model.

```
scribe artifacts [<meeting>]                 # render the review view
scribe artifacts [<meeting>] --approve <id>…  # build queued candidate(s)
scribe artifacts [<meeting>] --approve-all
scribe artifacts --all                        # bounded summary across meetings
```

Render groups, **all always printed** even at zero — a stable shape is far
easier for a pane (and a future parser) to consume than a variable one:

- **BUILT** — built or approved. Show the built path and the review-task state,
  joined from the run log.
- **QUEUED** — approvable. Exactly the over-cap and weaker-trigger dispositions.
- **disabled-type** — informational; not approvable.
- **OTHER** — catch-all for an unrecognized disposition, so nothing can vanish
  from the accounting.

Print a copy-pasteable approve hint listing the queued ids.

**Join keys are load-bearing.** When looking up a candidate's build outcome in the shared
run log, key on `(note_path, type, topic)` — not `(type, topic)`. Recurring
meetings legitimately repeat a topic weekly; a two-field key makes one week's
BUILT line satisfy every week's row, reporting an unbuilt candidate as built and
pointing at another meeting's artifact. Scan the log in reverse so the most
recent outcome for a key wins (correct for a retried build).

**When joining against a task file, match the path as a delimited token, not a
bare substring** — `docs/out.md` is a substring of `archive/docs/out.md`. And if
several rows reference the same path, report "still needs review" if *any* row is
open; reporting "done" while an open row exists is the unsafe direction.

### 4f. Approval mechanics

- **Resolve every requested id first; dispatch nothing until all resolve.** One
  bad id must produce zero builds.
- Resolving an id that is not queued (already built, or a disabled type) is an
  **error**, not a silent skip. Errors go to **stderr**, print nothing on
  stdout, and use a distinct exit status. Include the list of valid queued ids
  so the operator can recover.
- **Record the approval before dispatching**, not after. The local write is cheap
  and reversible; the detached build is neither. If the record fails, do not
  build.
- On a per-id failure mid-batch: warn naming the id, skip that id, **continue**
  with the rest, and exit non-zero at the end. Aborting the loop silently
  abandons ids that were already validated.
- Approval is **light** — no ceremony, no initials. The output is an unsent
  draft; the meaningful gate is the human reading it afterward. (If a deployment
  needs a heavier gate before *sending*, that belongs downstream, not here.)

### 4g. Mutating the sidecar safely

One writer only. Everything else is read-only, and must not create the sidecar
as a side effect of reading it.

**Do a targeted edit on the raw file text.** Do not round-trip through your
parsed representation. A tolerant parser that pads short rows and truncates
over-long ones is correct for rendering and **catastrophic** for writing: editing
one row then rewrites every other row from the lossy form, padding some and
permanently deleting the trailing fields of others — silent, irreversible loss on
rows the operation never targeted.

So: read the raw text, find the line whose first field is the target id, replace
only that line's disposition field, and write every other line back byte-for-byte
— including short rows, over-long rows, and blank lines.

- Zero matching lines → unknown-id error.
- **More than one matching line → error.** A duplicate id means a corrupt file;
  refuse rather than guessing which to edit.
- Validate the new disposition against the closed vocabulary (an `argparse`
  `choices=` or equivalent). An unvalidated value lets a caller write an
  arbitrary string — or one containing a separator, which shifts that row's
  columns or injects a fabricated row.
- Write atomically: temp file in the **same directory**, flush, fsync, then
  atomic rename over the target. Copy the original file's mode onto the temp
  file before the rename, or the first edit silently narrows the file's
  permissions to the temp default.

### 4h. The pane

Open a review pane after `stop` when running inside the multiplexer. It renders
the same view and accepts an inline command loop:
`approve <id>… | all | refresh | quit`.

- **Zero model calls.** The loop is a read-and-prompt loop. Assert this with a
  test that scans the script source for a model invocation.
- **EOF on stdin (pane closed) exits 0**, not an error.
- Unknown input re-prompts; it never exits or spins.
- Use a safe read that does not word-split or glob-expand operator input.
- **Purely additive.** It must not be able to make `stop` fail, hang, or alter
  anything that ran before it. Wrap **every** multiplexer call in a hard
  timeout with a kill-after — a multiplexer binary that ignores the initial
  signal will otherwise hang `stop` forever and orphan a process. Guard every
  command substitution so a no-match cannot abort the script.
- Always print a one-line fallback hint naming the `artifacts` command, whether
  or not the pane opened.

**Known limitation worth designing around:** builds land minutes after `stop`,
so at the instant the pane opens, every row reads "building" with no path. A
pane that never refreshes is, at the moment it appears, as uninformative as a
notification. Consider a bounded auto-refresh, or at minimum say in the pane how
to refresh.

### 4i. Back-compat for meetings that predate the sidecar

Every meeting captured before this feature exists has no sidecar. If the review
view answers "no record" for all of them, the feature is useless on the
operator's entire history.

**Build a log-derived fallback:** with no sidecar, reconstruct the BUILT group
from the run log, under a banner stating (a) the view is log-derived and
(b) skipped candidates **cannot** be recovered or approved for it, because the
log kept a topic but not the scope/audience needed to rebuild. Never let the
operator infer that nothing was skipped.

Three requirements that are easy to get wrong and were all caught in review:

1. **Distinguish "file absent" from "file present but empty."** A tolerant reader
   returns an empty list for both. Only *absent* may trigger reconstruction —
   because a meeting whose classifier proposed nothing legitimately writes a
   zero-row sidecar. Get this wrong and a brand-new zero-candidate meeting
   displays the *previous* meeting's artifacts under the new meeting's id, under
   a banner falsely claiming the meeting predates the sidecar. Also route
   "present but unreadable" (a directory at the path, a permission fault, a
   decode error) to the *present* branch: something is known to exist there.
2. **Select exactly one source group and name it.** Reconstruction has no
   meeting→note mapping, so it must pick a single group (e.g. the note owning the
   log's last relevant line) and **print that source in the banner and in each
   row**. A self-identifying view makes misattribution impossible to miss.
   Keep the per-row label short — a full path in a fixed-width column wraps the
   row and defeats the readability the view exists for; a basename plus the full
   path in the banner reads far better.
3. **Synthesize visibly different ids** (e.g. an `L` prefix) and make the
   approve path **reject them** with a message explaining they belong to a
   reconstructed, non-approvable view. Never let a reconstructed row be mistaken
   for an approvable one.

Residual to document rather than hide: an unresolvable/typo'd meeting id looks
identical to a genuinely pre-sidecar meeting — both reconstruct. A short "no
record for '<id>'" when the meeting resolver matches nothing closes it.

### 4j. Tests for §4

Cover at minimum: cap precedence and malformed values (inline comment, zero,
negative, duplicate key); the list/key reader disjointness; sidecar written for
every disposition with ids assigned across skipped candidates; separator
sanitization for the full line-break class; sidecar write failure does not
suppress classification output; the three-field log join under a repeated topic;
delimited-token task matching; open-wins task status; resolve rejects non-queued
and unknown ids with nothing on stdout; approve records before dispatching;
one bad id dispatches zero builds; per-id failure continues the batch and exits
non-zero; refusal when the note does not resolve leaves the row untouched;
raw-text edit preserves short/over-long/blank lines byte-for-byte; duplicate id
refused; atomic write leaves the original intact on failure; reconstruction fires
only for an absent sidecar; reconstructed ids rejected by approve; the pane's
approve and all branches (assert on the reconstructed argv, so id-slicing
regressions are caught); the pane makes no model call.

---

## 5. SEAM — An optional post-note gate

**This is the seam that lets a downstream layer add policy without core knowing
any policy.** Core ships the mechanism and no rules.

**Contract:** after the note is written and before the in-memory transcript is
destroyed, if a gate command is configured, run it with the note path and the
meeting directory. Interpret its exit status:

| Result | Core's action |
|---|---|
| success | proceed: destroy the transcript, write the audit line |
| a distinct "hold" status | **do not destroy** — move the meeting directory to a configured quarantine location and record why |
| any other failure / timeout | treat as hold — **fail closed in both directions** |

Then, and only then, run `SCRIBE_ON_STOP`.

Requirements:

- **Fail closed both ways:** never destroy when the gate is unclear, and never
  silently retain when the gate approved destruction. Both directions are
  failures.
- Every outcome writes one line to an append-only audit file: what happened,
  when, which meeting, and the gate's verdict.
- Unconfigured gate → today's behavior exactly (destroy, audit, hook).
- Hard timeout on the gate. A hung gate must not strand a meeting.
- Core defines **no** vocabulary for *why* a gate holds. It records the gate's
  own message verbatim and takes no view on it.

**Tests:** clear → destroyed + audited; hold → quarantined, not destroyed,
audited; gate crash → treated as hold; gate timeout → treated as hold;
unconfigured → destroyed as today; the quarantine path never lands inside the
RAM root.

---

## 6. CORE — Hardening catalog

These are real defects found in production or review of the mature deployment.
Every one is cheap to prevent and expensive to discover. Treat this as a
checklist, not background reading.

### 6.1 The lock-before-stdin deadlock (test-harness bug that hangs CI)

If a worker **acquires a file lock and then reads stdin**, a test that starts two
such workers and feeds them sequentially deadlocks about half the time: whichever
process wins the lock holds it while waiting for input the test will not send
until the other process exits — and that one cannot get the lock.

Feed and close **both** stdins first, then wait on both. Also assert exit status
with a timeout, so a future regression fails loudly instead of hanging the suite.
This bug intermittently hung an entire test run and looked exactly like flake.

### 6.2 `set -euo pipefail` traps

- A function whose **last statement** is `[[ cond ]] && cmd` returns 1 when the
  condition is false. Called from inside an `if` **body** (not its condition),
  that trips `errexit` and aborts before any following `return 0`. End such
  functions with an unconditional `return 0`.
- `x=$(cmd | grep -o …)` aborts the script when `grep` matches nothing, because
  `pipefail` propagates it. Guard every such assignment (`|| true`) — including
  ones already in the codebase; audit for them.
- A no-match glob makes `ls` fail. Enumerate with an array and test for a real
  match instead of parsing `ls`; and never build a glob out of user input, or a
  metacharacter changes the match semantics.
- Expanding an **empty array** under `set -u` errors. Guard on length first.
- `mkdir -p` as the last command of a top-level `&&` list aborts on failure
  before anything downstream runs (see §4c).
- Quote interpolations into command strings handed to another process.

### 6.3 Normalize once, use everywhere

If a machine-readable stdout line and a persisted record are produced from the
same values, **normalize once and use the same variables for both.** The mature
version cleaned only the persisted copy; because the build path took its topic
from stdout, a trailing space made the built-artifact lookup miss and the path
never surfaced — for the *majority* (auto-built) case. Fixing it also closed a
separator-injection hazard on the stdout reader and an exact-equality audience
comparison that a padded value had been silently failing.

### 6.4 Ambiguity is not a default

Several bugs were "picked the newest silently": a substring meeting match
resolving to more than one file, a duplicate id, a topic key matching two
meetings. **Say something** whenever a selection was ambiguous, and name what
was chosen. Silence here reads as certainty.

### 6.5 Bound anything unbounded

A "recent summary" that iterates every file grows forever. Bound it, and
**state the bound in the output** — a silent truncation reads as completeness.

### 6.6 Follow the symlink question

An existence check that follows symlinks and swallows errors treats a broken
symlink as "absent." Decide deliberately which side you want, and write the
choice down.

---

## 7. OUT — What must not enter this repo

Do not build, and do not let the vocabulary leak into core code, tests,
examples, config, or docs:

- Confidentiality/sensitivity headers, classification banners, or any
  document-marking scheme.
- Preservation-hold checks, record-lifecycle classes, or destruction schedules
  as *policy*. Core ships only the §5 seam and no rules.
- Domain taxonomies — named work items, house document types, org-specific
  audience names, branded templates.
- Any specific generator's house style or output conventions.
- Any real person, organization, machine, or absolute path.

All of it attaches downstream through `SCRIBE_ON_STOP` (and, for destruction
policy, the §5 gate). Core stays a capture-and-drafting engine that knows
nothing about the domain it serves.

`scribe-notes` stays generic: attendees, decisions, owner-attributed action
items. No domain framing.

---

## 8. Suggested build order

Each step is independently shippable and testable.

1. **§6 hardening audit** of the existing tree first — cheapest, and it stops you
   building on sand. Include the §6.1 test pattern before you add a second
   concurrent writer.
2. **§3 per-meeting glossary** — small, self-contained, immediate quality win.
3. **§2 doc-to-text helper** — prerequisite for §1b.
4. **§1 two-tier analyst** — highest user-visible value. Ship 1a's trigger
   contract before 1b's worker.
5. **§5 gate seam** — small, and §4 depends on ordering relative to it.
6. **§4 auto-artifacts**, in this order: classify → cap → sidecar → build →
   render → resolve/mark → approve → pane → back-compat. Do not reorder;
   each stage's tests depend on the previous one's contract.
7. **Docs + `scribe.conf.example`** for every new variable.
8. **Sanitization gate**, human diff review, then publish.

---

## 9. New configuration (add to `scribe.conf.example` with neutral defaults)

Follow the existing `SCRIBE_*` naming and the file's "commented default"
convention. Everything below is optional; unset must reproduce today's behavior.

| Area | Purpose |
|---|---|
| Deep analyst | enable/disable, model command (falls back to the light one), timeout, corpus root(s) |
| Doc extraction | per-format extractor command overrides |
| Glossary | per-meeting injection on/off |
| Artifacts | master on/off, enabled type list, auto-build cap, sidecar directory, run-log path, review-task file path |
| Gate | gate command, quarantine directory, gate timeout, audit-log path |
| Pane | review-pane on/off |

Document for each: what unset does, and which direction a malformed value fails.

---

## 10. Gate compliance checklist for this work

1. `bash scripts/sanitization-gate.sh` → exit 0, zero HARD hits.
2. Review every SOFT hit by hand. This brief uses neutral vocabulary
   deliberately (`scope`, `artifact`, "gate", "quarantine") to keep SOFT noise
   low — keep new code and docs in the same register.
3. No absolute path naming a real user, machine, or organization anywhere,
   including test fixtures and example config.
4. Placeholder identities only in tests and docs (Alice, Bob, "Acme standup").
5. Neutral commit authorship.
6. Human diff review before the repo goes public or the topic is added.
