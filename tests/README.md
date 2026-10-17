# Resolve-behaviour experiment

Answers one question with evidence rather than inference: **what does Google actually do to a
comment when it is resolved?** Everything runs against throwaway Drive files it creates and
deletes, never a real document.

Re-run it when Google changes something, or before trusting any claim in `SKILL.md` about
resolved comments.

## What it measures

```bash
python3 -I make_docx.py test.docx          # minimal .docx, 3 comments, nothing confidential
python3 -I make_docx.py done.docx --done 2 # same, comment 2 pre-marked w15:done="1"

python3 -I experiment.py setup             # upload as a stored .docx AND as a native Doc
python3 -I experiment.py state             # both sources, both files
python3 -I experiment.py resolve           # resolve via the Drive API (native only)
python3 -I experiment.py doneflag          # push done="1" and read it back
python3 -I experiment.py teardown          # trash both
```

The Drive API cannot resolve a comment on a stored `.docx`, because such a file has an **empty
Drive comment layer** until Docs populates it. The UI is therefore the only way to observe a real
resolve, which is what the Node scripts are for. They attach over CDP to a Chrome you started
with `--remote-debugging-port=9222` and are already signed in to:

```bash
node ui_history.mjs <fileId>                    # what the "Show all comments" panel shows
node ui_resolve.mjs <fileId> "<text fragment>"  # resolve one comment, verify in-session
```

`ui_resolve.mjs` dispatches the click on the element rather than at its coordinates, because a
floating toolbar intercepts a positional click, and it verifies the card actually left the open
pane before reporting success. Trusting the click without that check produced a false negative.

## What it found

| Test | Stored `.docx` | Native Google Doc |
|---|---|---|
| Comments visible to the Drive API on upload | **0 threads** | all 3 |
| Resolve in the Docs UI | comment **deleted**, 3 become 2 | `resolved: true`, intact |
| `w15:done="1"` set by hand | Docs **renders** it under "Resolved" | n/a |
| That flagged comment after Docs next saves | **gone** | n/a |
| Resolved comment after a later edit | n/a | still there |

Conclusion: Docs reads `w15:done` and never writes it, and discards resolved comments when it
saves a `.docx`. A stored `.docx` cannot retain them. Create review documents as native Docs.

## Caveat

A fresh `.docx` has an empty Drive comment layer; a `.docx` that has been edited in Docs for a
while does have one, and it can hold comments **already deleted from the file**. Check both
sources before concluding anything about a document that has been in review.
