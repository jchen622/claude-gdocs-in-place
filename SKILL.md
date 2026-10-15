---
name: gdocs-in-place
description: Edit a stored .docx on Google Drive in place on its existing file id, preserving comment threads and tracked changes, with tracked-change tooling and a pre-push verification gate. Use when changing a Drive-hosted .docx, especially one under author review, and above all when a local build pipeline generates it - rebuilding and re-uploading destroys every comment. Also carries the in-place rule for Drive generally; for native Google Slides use the gslides skill, for native Google Sheets use gsheets, for native Google Docs use gdocs.
---

# Editing Drive files in place

**Never replace a Drive file with a regenerated copy. Always write to the existing file id.**

The id carries everything that is not text: comment threads and their replies, @-mentions and
the notifications they send, resolution state, the share list, and every link anyone has pasted
into Slack, email or another document. Replacing the file destroys all of it, and the text can
look perfect while it happens.

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

`drive_io.py` will refuse a native file rather than let you treat it as an archive.

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

## Two things the tooling refuses to do

Both are failures that write nothing, because both destroy other people's work in ways that are
invisible afterwards.

**It will not edit inside another person's pending tracked change or Google Docs suggestion.**
Rewriting over a suggestion strips their authorship from the revision record. If a reviewer's
correction needs repositioning, accept it first, then move the settled text. Better word order is
not worth erasing who caught the problem.

**It will not edit text a comment is anchored to.** Deleting an anchor breaks the thread, often
one the reviewer is waiting on an answer to.

When you hit either refusal, say so and leave the content alone rather than working around it.

## Platform behaviour that will cost you time

- **Google Docs renumbers comment ids on save.** Ten comments with ids 0,1,2,5,7,11,15,20,21,23
  came back as 0 to 8. Never key anything on comment id. Match on author plus text.
- **Resolving a comment deletes it** from an exported `.docx`; there is no resolved flag. The
  live file's comment set *is* the set of open comments, so a drop in count is normal and does
  not mean something broke.
- **The Drive comments API returns nothing** for a stored `.docx`, because the comments live in
  the file's OOXML rather than Drive's comment layer. Read them from `word/comments.xml`.
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

## Before you push, ask

Is the id the same? Does `verify_edit.py` exit 0? Did I avoid consuming anyone's pending change
or comment anchor? If a document is under review, is every change tracked and attributed to the
person who directed it, so each can be accepted or rejected independently?
