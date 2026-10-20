---
name: gdocs-in-place
description: Edit a stored .docx on Google Drive in place on its existing file id, preserving comment threads and tracked changes, with tracked-change tooling and a pre-push verification gate. Use when changing a Drive-hosted .docx, especially one under author review, and above all when a local build pipeline generates it - rebuilding and re-uploading destroys every comment. Also carries the in-place rule for Drive generally; for native Google Slides use the gslides skill, for native Google Sheets use gsheets, for native Google Docs use gdocs.
---

# Editing Drive files in place

## Setup, first run

Python 3 and its standard library, nothing to install. The one requirement is a Drive-scoped
gcloud login:

```bash
gcloud auth login --enable-gdrive-access
python3 -I scripts/drive_io.py stat <file_id>     # confirms the token reaches Drive
```

Without `--enable-gdrive-access` every call fails with `could not get an access token`, or
returns 403 on a file id that is perfectly valid. Tokens last about an hour.

Shared Drive files need nothing extra: every request already sends `supportsAllDrives=true`.

After changing anything under `scripts/`, run the suite:

```bash
python3 -I tests/guards_test.py
```

**Never replace a Drive file with a regenerated copy. Always write to the existing file id.**

The id carries everything that is not text: comment threads and their replies, @-mentions and
the notifications they send, resolution state, the share list, and every link anyone has pasted
into Slack, email or another document. Replacing the file destroys all of it, and the text can
look perfect while it happens.

## Before you start: a document heading into review should be a native Google Doc

**If a document will go through multi-reviewer review where comments get resolved, create it as a
native Google Doc, not a stored `.docx`.** This is a decision made once, at creation, and it
cannot be undone cheaply later: converting produces a **new file id**, which is exactly what the
in-place rule exists to protect.

The reason is measured on purpose-built throwaway documents, resolving through the Docs UI in
both cases, not inferred:

| | Stored `.docx` | Native Google Doc |
|---|---|---|
| Resolve a comment | **deleted from the file**, 3 comments become 2 | kept, `resolved: true` |
| Author and text afterwards | **stripped** from Drive's tombstone | intact |
| Replies afterwards | **gone** | intact, including the resolution reply |
| Visible in comment history | **no** | yes, under a "Resolved" heading |
| Survives a later edit | n/a, already gone | yes |

On a stored `.docx`, resolving is recorded as a *deletion*. Drive's comment layer keeps a
tombstone row with `author: null` and empty content, and the file loses the comment outright. One
manuscript lost **68 comments** that way across a single review round, and they were only
recoverable because Drive still held un-purged revisions.

So: native Doc for anything under review. Reach for this skill's `.docx` tooling when the file is
already a `.docx` and converting would cost an id people have linked to.

### Converting an existing `.docx`, and what it costs

**There is no in-place conversion.** Both routes were tried and neither changes the mimeType of
an existing file: `PATCH /files/<id>` with a new `mimeType` is accepted and silently ignored,
returning the old type, and `PATCH /upload/files/<id>?uploadType=media` with the Docs mimeType
fails `HTTP 400 Invalid MIME type provided for the uploaded content`. Conversion only happens on
ingest, via `files.copy` with `mimeType` set or a fresh upload, and both mint a **new file id**.

What a converting copy keeps and loses, measured:

| | Survives? |
|---|---|
| Comments, with their authors | **yes**, all of them |
| Comment anchors | **yes**, 3 of 3, even though `quotedFileContent` reports empty over the API |
| Tracked insertions and deletions | **yes**, accept and reject views both correct |
| The file id, so existing links keep working | **no** |
| The share list | **no**: a source shared to a whole domain copied to owner-only |
| Comments already deleted by an earlier resolve | **no**, those are gone regardless |

So the content survives conversion intact; what you pay is the id and re-sharing with every
collaborator. Decide this at creation, not mid-review.

## Which skill to use, by file type

**This skill handles a stored `.docx` on Drive.** Native Google files are a different thing
entirely: they have no OOXML to edit, and their comments live in Drive's comment layer rather
than inside the file. Each has its own working path, already built:

| File | Use | Why not this skill |
|---|---|---|
| Stored `.docx` | **this skill** | - |
| Native Google Slides | **`gslides`** (Apps Script bridge) | the Slides REST API needs a quota project and is unavailable here; a `.pptx` round trip corrupts Material Icons |
| Native Google Sheets | **`gsheets`** (Sheets API) | values and ranges, not OOXML |
| Native Google Doc | **`gdocs`** | Docs API, not OOXML |

Those three are separate skills and are **not installed by this one**. If you do not have them,
treat the rows above as "this skill is the wrong tool" rather than as a working alternative: the
important half is that a native file must not be pushed as OOXML, not which skill you reach for
instead.

`drive_io.py` refuses a native file on both sides. `pull` will not hand you one to treat as an
archive, and `push` checks the target's own `mimeType` before uploading, so a `.docx` cannot be
PATCHed onto a native Doc or a `.pptx` onto a `.docx` id.

All of them edit in place on the existing id. **The rule below is Drive-wide; only the tooling
in this skill is `.docx`-specific.**

## When this matters most

The dangerous case is a document with a **local source of truth**. When a pipeline generates the
`.docx` or `.pptx` from markdown, the reflex is to rebuild and upload. That regenerates the body
and **deletes every comment and tracked change on it**.

The incident this skill is built from: a document under multi-reviewer review carried **hundreds
of comments and tracked changes**. A single rebuild-and-upload would have erased the entire
review round, and the resulting file would have looked completely normal.

**While a document is under review, the build pipeline is suspended.** Edit the XML inside the
existing archive and PATCH it back.

## The loop

```bash
S=~/.claude/skills/gdocs-in-place/scripts

python3 $S/drive_io.py stat <file_id>                  # what am I dealing with?
python3 $S/drive_io.py pull <file_id> before.docx      # always snapshot first

python3 $S/tracked_edit.py before.docx after.docx --author "Full Name" \
    --replace "old wording" "new wording" \
    --insert-after "anchor text" " added text"

python3 $S/verify_edit.py before.docx after.docx --expect-changed 1   # must exit 0

python3 $S/drive_io.py push <file_id> after.docx       # same id, never a new file
python3 $S/drive_io.py pull <file_id> check.docx       # confirm it landed
```

`push` **refuses by default when Drive has moved since the pull this build came from**, comparing
against the ledger that `pull` wrote. That guard exists because a push built on a stale pull once
restored a sentence the author had deleted and resurrected two comments they had resolved, and the
evidence was already in the output: `previous_modifiedTime` did not match the pull. Pass
`--expect <modifiedTime>` to pin the base explicitly, or `--force` to override, which should be
rare and deliberate.

`verify_edit.py` is the gate, not a formality. It fails the push on any of:

1. **comments not identical item by item.** A matching count hides one comment being destroyed
   while another is resurrected.
2. **comment anchor spans changed.** A disturbed anchor silently widens, usually to the whole
   paragraph, and the reviewer loses the phrase they marked.
3. **reject view not byte-identical.** The strongest single check. It proves the edit only
   *proposed* changes and did not alter settled text. If it fails, whoever rejects your change
   will not get back what they had.
4. **more paragraphs changed than expected**, so an edit meant for one sentence has not quietly
   rewritten five.

## What the tooling refuses to do

Every one of these is a failure that writes nothing, because each destroys work in a way that is
invisible afterwards. All are covered by `tests/guards_test.py`.

**It will not edit inside another person's pending tracked change or Google Docs suggestion.**
Rewriting over a suggestion strips their authorship from the revision record. If a reviewer's
correction needs repositioning, accept it first, then move the settled text. Better word order is
not worth erasing who caught the problem. This covers a `w:ins` or `w:del` around the run, a
block-level `w:sdt` wrapping the whole paragraph, and a pending change on the paragraph *mark*
in `w:pPr/w:rPr`, which no scan of the text can see.

**It will not edit text a comment is anchored to.** Deleting an anchor breaks the thread, often
one the reviewer is waiting on an answer to. The check is positional: the character range of the
edit is compared against the character range of each anchor in that paragraph. Comparing the
strings instead both over-refuses, blocking an unanchored copy of an anchored word elsewhere in
the paragraph, and under-refuses, since a short anchor was skipped by a length test.

**It will not guess between several matches.** If the text appears more than once, the run stops
and lists the candidates rather than editing the first one. Pass `--occurrence N` to choose, or
give more surrounding text. Silently taking the first match is how an edit lands in the wrong
section while `--expect-changed 1` still reports success.

**It will not split a run that holds more than plain text.** A run can carry a tab, a line break,
a field, a footnote reference or an image alongside its text. Rebuilding such a run from its
`w:t` alone drops those, and a text-level check cannot see it happen: one run reading
`See⟶Table 1` came back as `Refer toSee`, with both the tab and "Table 1" gone.

**It will not take an empty `OLD` or `ANCHOR`.** An empty string matches at the start of the
first run, so it would insert at the top of the document rather than where you meant.

**It will not write its output over its own input.** `rewrite(src, dst)` with the same path
truncates the file while the zip is still being read from it.

When you hit a refusal, say so and leave the content alone rather than working around it.

## Platform behaviour that will cost you time

- **Google Docs renumbers comment ids on save.** Ten comments with ids 0,1,2,5,7,11,15,20,21,23
  came back as 0 to 8. Never key anything on comment id. Match on author plus text.
- **Resolving a comment deletes it** from a stored `.docx`, measured on throwaway files rather
  than inferred: a document with three open comments, one resolved in the Docs UI, comes back
  with **two**, and no `w15:done="1"` is written. A drop in count is therefore normal and is
  **not** recoverable from the file. See "Resolved comments are destroyed" below; this is the
  single most expensive behaviour on this list.
- **The Drive comments API does work on a stored `.docx`.** An earlier version of this file
  claimed it returns nothing. It does not: `files/<id>/comments?includeDeleted=true` returns the
  threads, and it holds **replies that never appear in the OOXML at all**, so archive both
  sources, not one. What it cannot do is recover anything deleted: deleted comments come back
  tombstoned, `author: null` and empty `content`.
- **Docs re-segments runs on save**, so tracked-change counts shift without any content change.
  Compare the accept and reject views, never the raw `w:ins` / `w:del` counts.
- **Docs wraps suggestions in `w:sdt`**, which is why protected-run detection checks for it.
- **Mid-edit autosaves look exactly like edits.** A pull can catch a half-typed keystroke, so a
  word arrives with a stray character welded to it or two words run together mid-replacement.
  A single stray character at a word boundary is an autosave artifact until the author confirms
  otherwise, not an edit to fold in. Ask; re-pulling a minute later costs nothing. Three such
  artifacts appeared in one session, and each would have been folded in as a real edit.
- **Access tokens expire mid-session.** A push then returns an empty body, curl exits 0, and the
  next step reports success against a file that was never written. `drive_io.py` checks the HTTP
  status; if you call the API by hand, check it yourself and re-verify on Drive.
- **Google-native files** (Docs, Sheets, Slides proper) must be exported with
  `/export?mimeType=...`; `alt=media` fails on them. `drive_io.py stat` tells you which you have.

## Resolved comments are destroyed, so capture them before they go

On a stored `.docx` there is no resolved state to read back. The moment a reviewer resolves a
thread it is gone from the file, and Drive's tombstone keeps no author and no text. **Nothing in
this tooling causes that**: `rewrite()` replaces `word/document.xml` and copies every other zip
entry byte-identical, and `verify_edit.py` fails any push whose comment set differs. A drop
between two pulls is always a human resolving one.

### Do not try to fix this by writing `w15:done="1"`

It looks like it works, and it does not. Measured on disposable Drive files:

| Test | Result |
|---|---|
| Upload a `.docx` with `w15:done="1"` on one comment | Docs **renders it** under a "Resolved" heading, author and text intact |
| Resolve a comment in the Docs UI on a `.docx` | comment is **deleted** from the file; no `done` flag written |
| Then re-read the file | the hand-set `done="1"` comment is **also gone** |
| Same resolve on a **native Google Doc** | `resolved: true`, `deleted: false`, text and replies intact, survives later edits |

So Docs *reads* the flag and never *writes* it, and its next save of the file discards every
resolved comment including ones flagged by hand. Re-injecting resolved comments therefore buys
one page load, not persistence. The only durable answer is the native Google Doc above.

What the tooling does instead is capture:

```bash
python3 drive_io.py pull <id> out.docx   # archives comments from BOTH sources, every time
python3 drive_io.py lost <id>            # everything ever seen that is no longer present
python3 drive_io.py pin  <id>            # stop revision content expiring (do this early)
```

`pull` folds every comment it sees into an append-only ledger (`~/.gdocs-in-place/ledger/<id>.json`,
override with `GDOCS_LEDGER_DIR`) and prints `resolved_since_last_pull` when something has gone.
**Pull often while a document is under review**: a comment resolved between two pulls was never
captured, and the ledger narrows that window rather than closing it.

**`pin` is the part people skip, and it does more than it looks like it does.** Revision history
is the only way back to a resolved comment, so pin at the start of a review rather than when
something is already missing.

**A 403 on an old revision does not mean the content is gone.** Downloading an unpinned revision
older than about 30 days returns `HTTP 403`, which reads exactly like a purge. It is not: setting
`keepForever` on that same revision makes it download fine. Drive is gating access, not deleting
bytes. On the manuscript this skill came from, 32 revisions returned 403, and pinning brought all
of them back, which took the recoverable history from 128 comments to **152** and the author's own
recovered replies from 3 to 8. So **always `pin` before concluding anything is unrecoverable**,
and never read a 403 as proof of loss.

One thing that genuinely is not recoverable, worth saying out loud rather than discovering later:
**a reply written and resolved inside one editing session**. Drive snapshots on autosave
boundaries, so it never existed at a snapshot point and no amount of pinning will produce it.

## Before you push, ask

Is the id the same? Does `verify_edit.py` exit 0? Did I avoid consuming anyone's pending change
or comment anchor? If a document is under review, is every change tracked and attributed to the
person who directed it, so each can be accepted or rejected independently?
