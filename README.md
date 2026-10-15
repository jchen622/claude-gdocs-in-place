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
| `drive_io.py` | pull, push and stat by file id, never creating a new file |
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
