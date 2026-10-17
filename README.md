# gdocs-in-place

A [Claude Code](https://claude.com/claude-code) skill for editing Google Drive documents **in
place**, on their existing file id, so that comment threads, tracked changes and share links
survive the edit.

## The problem

Replacing a Drive file with a regenerated copy destroys everything that is not text: comment
threads and their replies, @-mentions and the notifications they send, resolution state, the
share list, and every link anyone has pasted elsewhere. The new file looks perfect. The loss is
silent.

This bites hardest when a document has a **local source of truth**. If a build pipeline
generates the `.docx` from markdown, the natural move is to rebuild and upload, and that deletes
every comment and tracked change in one step. The skill exists because a document under
multi-reviewer review carried hundreds of comments and tracked changes, any one rebuild away
from being erased.

## What it provides

| Script | Purpose |
|---|---|
| `drive_io.py` | pull, push, stat, pin and lost by file id, never creating a new file |
| `ooxml.py` | read a `.docx`'s accept / reject / raw views, comments and anchor spans |
| `tracked_edit.py` | apply `w:del`/`w:ins` tracked changes by text match, splitting runs and preserving formatting |
| `verify_edit.py` | the pre-push gate |

### The gate

`verify_edit.py` refuses the push unless all of these hold:

1. **comments identical item by item**, not merely the same count, since a count match hides one
   comment being destroyed while another is resurrected;
2. **comment anchor spans unchanged**, since a disturbed anchor silently widens to the whole
   paragraph;
3. **the reject view byte-identical**, which proves the edit only *proposed* changes rather than
   altering settled text;
4. **no more paragraphs changed than expected.**

### Two refusals

`tracked_edit.py` writes nothing and exits non-zero rather than:

- **editing inside another person's pending tracked change or suggestion**, which would strip
  their authorship out of the revision record; or
- **editing text a comment is anchored to**, which would break a thread someone may be waiting
  on.

Both destroy other people's work in ways that are invisible afterwards, so neither is a warning.

### Resolved comments

On a stored `.docx`, resolving a comment **deletes** it. The OOXML resolved flag
(`w15:done="1"`) exists but Google never writes it, and Drive's comment layer keeps only a
tombstone with the author and text stripped. One manuscript lost 68 comments this way in a single
review round.

Nothing in this tooling causes that: `rewrite()` touches only `word/document.xml`, and the gate
fails any push whose comment set differs. So the tooling captures instead. `pull` folds every
comment it sees, from the file *and* from Drive's comment layer, into an append-only ledger and
reports anything that has disappeared since the previous pull. `pin` sets `keepForever` on the
file's revisions, because revision history is the only route back to a resolved comment.

Pinning also *restores access*: an unpinned revision older than about 30 days returns `HTTP 403`
on download, which looks exactly like a purge and is not one. Pin it and the same revision reads
fine. On the document this was built for, pinning took the recoverable comment history from 128
to 152. **Never treat a 403 on an old revision as proof the content is gone.**

**The real fix is upstream:** a document heading into multi-reviewer review should be created as
a **native Google Doc**, which retains resolved comments in full, with their replies, permanently.
Converting later is not equivalent, because it mints a new file id.

### Staleness

`push` refuses when Drive has moved since the pull the build came from. A push built on a stale
pull once restored a deleted sentence and resurrected two resolved comments.

## Usage

```bash
S=path/to/gdocs-in-place/scripts

python3 $S/drive_io.py pull <file_id> before.docx

python3 $S/tracked_edit.py before.docx after.docx --author "Full Name" \
    --replace "old wording" "new wording"

python3 $S/verify_edit.py before.docx after.docx --expect-changed 1   # must exit 0

python3 $S/drive_io.py push <file_id> after.docx
```

Authentication uses `gcloud auth print-access-token`. Python standard library only.

## Installation

Copy the directory into `~/.claude/skills/`. Claude Code picks it up from the frontmatter in
`SKILL.md`.

## Notes

`SKILL.md` also records platform behaviour that is easy to lose time to: Google Docs renumbers
comment ids on save, resolving a comment deletes it from the exported file, Docs re-segments
runs so tracked-change counts shift without content changing, suggestions arrive wrapped in
`w:sdt`, mid-edit autosaves can look exactly like deliberate edits, and an expired access token
makes a push fail silently while curl still exits 0.

## Licence

MIT.
