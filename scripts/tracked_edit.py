#!/usr/bin/env python3
"""Apply tracked changes to a .docx by matching text, so a reviewer can accept or reject each.

    python3 tracked_edit.py IN.docx OUT.docx --author "Jane Doe" \
        --replace "old wording" "new wording" \
        --replace "another"    ""                # deletion
        --insert-after "anchor text" " added"    # pure insertion

Each --replace becomes a `w:del` holding the old text followed by a `w:ins` holding the new,
both carrying the author and a timestamp. --insert-after emits only a `w:ins`, which is the
better choice when nothing is being removed: it leaves a smaller, more readable diff.

Three things this gets right that hand-rolled XML usually does not:

  * **Run splitting with formatting preserved.** A match rarely lines up with a run boundary, so
    the run is split into head / target / tail and each piece keeps a copy of the original
    `rPr`. Without this an edit silently drops bold, superscripts or font changes.
  * **It refuses to touch another person's pending change.** Text inside an existing `w:ins` or
    `w:del`, or inside a Google Docs `w:sdt` suggestion wrapper, is skipped. Rewriting over
    someone's suggestion strips their authorship out of the revision record.
  * **It refuses to break a comment anchor.** If a match overlaps the text a comment is anchored
    to, the edit is rejected rather than silently orphaning the thread.

Both refusals are failures, not warnings: the run exits non-zero and writes nothing.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import os
import re
import sys
import xml.etree.ElementTree as ET

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))  # keep `python3 -I` working

from ooxml import W, XS, document, anchors_by_paragraph, rewrite

ET.register_namespace('w', 'http://schemas.openxmlformats.org/wordprocessingml/2006/main')


def _next_id(root) -> int:
    ids = [int(e.get(W + 'id')) for e in root.iter()
           if e.tag in (W + 'ins', W + 'del') and (e.get(W + 'id') or '').isdigit()]
    return max(ids or [0]) + 1000


def _protected(para) -> set:
    """Runs we must not rewrite: inside someone's pending change, or a Docs suggestion."""
    out = set()
    for holder in list(para.iter(W + 'ins')) + list(para.iter(W + 'del')) + \
            list(para.iter(W + 'sdt')):
        for r in holder.iter(W + 'r'):
            out.add(id(r))
    return out


def _para_mark_changed(para) -> bool:
    """True when the paragraph *mark* itself carries a pending insertion or deletion.

    Lives in `w:pPr/w:rPr/w:ins|w:del` rather than in the body, so a scan of the runs misses it
    entirely. Editing inside such a paragraph mixes our change into someone else's pending
    paragraph split or merge, and accepting one then does something nobody proposed.
    """
    pPr = para.find(W + 'pPr')
    if pPr is None:
        return False
    rPr = pPr.find(W + 'rPr')
    if rPr is None:
        return False
    return rPr.find(W + 'ins') is not None or rPr.find(W + 'del') is not None


def _strip_rpr_change(rpr):
    """Copy `rPr` without its `w:rPrChange`.

    `w:rPrChange` records a *formatting* revision and carries a revision id. Splitting a run
    deep-copies its `rPr` into every fragment, so without this the one revision id reappears
    three or four times and Word reads it as several separate formatting changes.
    """
    if rpr is None:
        return None
    out = copy.deepcopy(rpr)
    for ch in list(out):
        if ch.tag == W + 'rPrChange':
            out.remove(ch)
    return out


# Run children that carry content a text-level view cannot see. A run holding any of these is
# not safely splittable by rewriting its single `w:t`, because the rebuild drops them.
_RUN_CONTENT = {W + 'tab', W + 'br', W + 'drawing', W + 'pict', W + 'sym', W + 'object',
                W + 'fldChar', W + 'instrText', W + 'footnoteReference', W + 'endnoteReference',
                W + 'commentReference', W + 'ptab', W + 'softHyphen', W + 'noBreakHyphen'}


def _unsplittable(run) -> str:
    """'' if the run is a plain single-text run, else why it must not be split."""
    texts = [c for c in run if c.tag in (W + 't', W + 'delText')]
    extra = sorted({c.tag.split('}')[-1] for c in run if c.tag in _RUN_CONTENT})
    if extra:
        return 'run also holds ' + ', '.join('w:' + e for e in extra)
    if len(texts) > 1:
        return f'run holds {len(texts)} separate text elements'
    return ''


def _scan(para):
    """Character-offset map of one paragraph.

    Returns `(text, runs, anchors)` where `text` is every character in the paragraph in document
    order, `runs` is `[(element, start, end, is_direct_child_of_para)]`, and `anchors` is the
    `[(start, end)]` character range each comment is anchored to.

    Offsets are what make the anchor check honest. Comparing strings ("does OLD contain an
    anchor, or an anchor contain OLD") misses a partial overlap entirely: an anchor on
    "quick brown" and an edit to "brown fox" share "brown", neither contains the other, and the
    edit silently eats half of what the reviewer marked.
    """
    parts: list[str] = []
    runs: list[tuple] = []
    anchors: list[tuple[int, int]] = []
    open_at: dict[str, int] = {}
    pos = 0

    def walk(node):
        nonlocal pos
        for ch in node:
            if ch.tag == W + 'commentRangeStart':
                open_at[ch.get(W + 'id')] = pos
            elif ch.tag == W + 'commentRangeEnd':
                start = open_at.pop(ch.get(W + 'id'), None)
                if start is not None:
                    anchors.append((start, pos))
            elif ch.tag == W + 'r':
                start = pos
                walk(ch)
                runs.append((ch, start, pos, node is para))
            elif ch.tag in (W + 't', W + 'delText'):
                t = ch.text or ''
                parts.append(t)
                pos += len(t)
            else:
                walk(ch)

    walk(para)
    # A range opened here and closing in a later paragraph still protects the rest of this one.
    for start in open_at.values():
        anchors.append((start, pos))
    return ''.join(parts), runs, anchors


class Editor:
    def __init__(self, root, author: str):
        self.root = root
        self.author = author
        self.date = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.nid = _next_id(root)
        self.para_anchors = anchors_by_paragraph(root)
        # ElementTree has no parent pointers; build one so a paragraph can be
        # inserted next to its sibling.
        self.parents = {id(c): parent for parent in root.iter() for c in parent}

    def _run(self, rpr, text, dele=False):
        r = ET.Element(W + 'r')
        if rpr is not None:
            r.append(copy.deepcopy(rpr))
        el = ET.SubElement(r, W + ('delText' if dele else 't'))
        el.text = text
        el.set(XS, 'preserve')
        return r

    def _wrap(self, tag, rpr, text, dele=False):
        self.nid += 1
        w = ET.Element(W + tag, {W + 'id': str(self.nid),
                                 W + 'author': self.author, W + 'date': self.date})
        w.append(self._run(rpr, text, dele))
        return w

    def new_paragraph(self, anchor: str, text: str) -> str:
        """Add a whole new paragraph after the one containing `anchor`.

        The paragraph mark itself is marked inserted, via `w:pPr/w:rPr/w:ins`. Without that the
        runs disappear on reject but the empty paragraph stays behind, which breaks the
        reject-view-byte-identical invariant and leaves a blank line in the reviewer's copy.
        """
        for para in self.root.iter(W + 'p'):
            if anchor not in ''.join(t.text or '' for t in para.iter(W + 't')):
                continue
            for a in self.para_anchors.get(id(para), []):
                if len(a) > 3 and (anchor in a or a in anchor):
                    return f'anchor paragraph carries a comment: {a[:60]!r}'
            parent = self.parents.get(id(para))
            if parent is None:
                return 'could not find the parent of the anchor paragraph'

            self.nid += 1
            np = ET.Element(W + 'p')
            src_pPr = para.find(W + 'pPr')
            pPr = copy.deepcopy(src_pPr) if src_pPr is not None else ET.Element(W + 'pPr')
            for child in list(pPr):                      # drop any inherited change marking
                if child.tag in (W + 'rPr',):
                    pPr.remove(child)
            rPr = ET.SubElement(pPr, W + 'rPr')
            ET.SubElement(rPr, W + 'ins', {W + 'id': str(self.nid), W + 'author': self.author,
                                           W + 'date': self.date})
            np.append(pPr)

            for i, seg in enumerate(re.split(r'\*([^*]+)\*', text)):
                if not seg:
                    continue
                rpr = None
                if i % 2:                                 # odd segments were *wrapped*
                    rpr = ET.Element(W + 'rPr')
                    ET.SubElement(rpr, W + 'i')
                np.append(self._wrap('ins', rpr, seg))

            idx = list(parent).index(para)
            parent.insert(idx + 1, np)
            return ''
        return f'no paragraph contains {anchor[:60]!r}'

    def delete_paragraph(self, anchor: str) -> str:
        """Mark a whole paragraph deleted, its paragraph mark included.

        Deleting only the runs leaves the paragraph mark behind, so accepting the change leaves a
        blank line where the text was. The mark is deleted via `w:pPr/w:rPr/w:del`, the mirror of
        the `w:ins` that `new_paragraph()` writes.

        Refuses a paragraph containing a hyperlink: the runs there are nested inside `w:hyperlink`
        rather than being direct children, and a `w:del` wrapped around the hyperlink instead of
        inside it is not what Word reads back.
        """
        for para in self.root.iter(W + 'p'):
            txt = ''.join(t.text or '' for t in para.iter(W + 't'))
            if anchor not in txt:
                continue
            for a in self.para_anchors.get(id(para), []):
                if len(a) > 3:
                    return f'paragraph carries a comment anchor: {a[:60]!r}'
            if list(para.iter(W + 'ins')) or list(para.iter(W + 'del')):
                return 'paragraph already carries a pending tracked change'
            if list(para.iter(W + 'hyperlink')):
                return 'paragraph contains a hyperlink; delete it by hand'

            pPr = para.find(W + 'pPr')
            if pPr is None:
                pPr = ET.Element(W + 'pPr')
                para.insert(0, pPr)
            rPr = pPr.find(W + 'rPr')
            if rPr is None:
                rPr = ET.SubElement(pPr, W + 'rPr')
            self.nid += 1
            ET.SubElement(rPr, W + 'del', {W + 'id': str(self.nid),
                                           W + 'author': self.author, W + 'date': self.date})

            for child in list(para):
                if child.tag != W + 'r':
                    continue
                idx = list(para).index(child)
                para.remove(child)
                for t in child.iter(W + 't'):
                    t.tag = W + 'delText'          # w:t is not legal inside w:del
                self.nid += 1
                d = ET.Element(W + 'del', {W + 'id': str(self.nid),
                                           W + 'author': self.author, W + 'date': self.date})
                d.append(child)
                para.insert(idx, d)
            return ''
        return f'no paragraph contains {anchor[:60]!r}'

    def _in_block_sdt(self, para) -> bool:
        """True when a structured-document-tag wraps the whole paragraph.

        A block-level `w:sdt` sits *above* `w:p`, so nothing inside the paragraph reveals it.
        Google Docs wraps a suggestion this way, and rewriting inside one takes the suggestion
        over in our name.
        """
        node = self.parents.get(id(para))
        while node is not None:
            if node.tag == W + 'sdt':
                return True
            node = self.parents.get(id(node))
        return False

    def find(self, old: str) -> list[tuple]:
        """Every occurrence of `old`, in document order, as `(para, text, runs, anchors, start)`."""
        hits = []
        for para in self.root.iter(W + 'p'):
            text, runs, anchors = _scan(para)
            for m in re.finditer(re.escape(old), text):
                hits.append((para, text, runs, anchors, m.start()))
        return hits

    def apply(self, old: str, new: str, *, insert_only=False, occurrence: int | None = None) -> str:
        """Returns '' on success or a reason string on refusal."""
        if not old:
            return ('OLD is empty. An empty string matches at offset 0 of the first run, so this '
                    'would insert at the top of the document rather than where you meant.')

        hits = self.find(old)
        if not hits:
            return f'no run contains {old[:60]!r}'
        if len(hits) > 1 and occurrence is None:
            where = '; '.join(
                f'{i + 1}: ...{t[max(0, s - 25):s + len(old) + 25]}...'
                for i, (_, t, _, _, s) in enumerate(hits[:5]))
            return (f'{len(hits)} occurrences of {old[:40]!r}; refusing to guess. '
                    f'Pass --occurrence N (1-based) or give more surrounding text. {where}')
        if occurrence is not None and not 1 <= occurrence <= len(hits):
            return f'--occurrence {occurrence} out of range: {len(hits)} occurrence(s) found'

        para, text, runs, anchors, start = hits[(occurrence or 1) - 1]
        end = start + len(old)

        if self._in_block_sdt(para):
            return 'paragraph is inside a w:sdt (a Docs suggestion); refusing to rewrite it'
        if _para_mark_changed(para):
            return 'paragraph mark carries a pending tracked change; refusing to rewrite it'

        # Positional overlap, not string containment. Touching at a boundary is not an overlap.
        for a_start, a_end in anchors:
            if max(a_start, start) < min(a_end, end):
                return (f'overlaps a comment anchor at characters {a_start}-{a_end}: '
                        f'{text[a_start:a_end][:60]!r}')

        guard = _protected(para)
        holder = next((r for r in runs if r[1] <= start and end <= r[2]), None)
        if holder is None:
            return (f'{old[:40]!r} spans more than one run, so splitting it would have to '
                    'rebuild several runs and their formatting. Edit a shorter span that sits '
                    'inside one run.')
        child, r_start, _r_end, direct = holder
        if not direct:
            return 'match sits in a nested run (hyperlink, field or suggestion); edit it by hand'
        if id(child) in guard:
            return "match sits inside someone else's pending change; refusing to rewrite it"
        why = _unsplittable(child)
        if why:
            return (f'cannot split this run without losing content: {why}. '
                    'Edit a span that sits in a plain text run.')

        t = child.find(W + 't')
        if t is None:
            return 'match sits in a deleted run; refusing to rewrite it'
        txt = t.text or ''
        i = start - r_start
        head, tail = txt[:i], txt[i + len(old):]
        rpr = _strip_rpr_change(child.find(W + 'rPr'))
        idx = list(para).index(child)
        para.remove(child)
        seq = []
        if head:
            seq.append(self._run(rpr, head))
        if insert_only:
            seq.append(self._run(rpr, old))
            seq.append(self._wrap('ins', rpr, new))
        else:
            seq.append(self._wrap('del', rpr, old, dele=True))
            if new:
                seq.append(self._wrap('ins', rpr, new))
        if tail:
            seq.append(self._run(rpr, tail))
        for off, el in enumerate(seq):
            para.insert(idx + off, el)
        return ''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--author', required=True)
    ap.add_argument('--replace', nargs=2, action='append', metavar=('OLD', 'NEW'), default=[])
    ap.add_argument('--insert-after', nargs=2, action='append',
                    metavar=('ANCHOR', 'TEXT'), default=[])
    ap.add_argument('--delete-paragraph', action='append', metavar='ANCHOR', default=[],
                    help='delete the whole paragraph containing ANCHOR, paragraph mark included. '
                         'Use when a paragraph is wholly redundant; --replace with an empty NEW '
                         'empties the text but leaves a blank line behind on accept.')
    ap.add_argument('--new-paragraph-after', nargs=2, action='append',
                    metavar=('ANCHOR', 'TEXT'), default=[],
                    help='add a whole new paragraph after the one containing ANCHOR, copying '
                         'its paragraph properties. Wrap italic spans in *asterisks*. Needed '
                         'for things like a reference-list entry, which --insert-after cannot '
                         'do because it can only add text inside an existing paragraph.')
    ap.add_argument('--occurrence', type=int, default=None, metavar='N',
                    help='when the match text appears more than once, edit the Nth (1-based) '
                         'instead of refusing. Applies to every --replace and --insert-after in '
                         'this run, so use one edit per invocation when you need it.')
    a = ap.parse_args()

    if os.path.exists(a.dst) and os.path.exists(a.src) and os.path.samefile(a.src, a.dst):
        print('src and dst are the same file; the output would truncate the input.',
              file=sys.stderr)
        return 2
    for label, vals in (('--replace OLD', [o for o, _ in a.replace]),
                        ('--insert-after ANCHOR', [x for x, _ in a.insert_after]),
                        ('--delete-paragraph ANCHOR', list(a.delete_paragraph)),
                        ('--new-paragraph-after ANCHOR', [x for x, _ in a.new_paragraph_after])):
        if any(not v.strip() for v in vals):
            print(f'{label} is empty or blank. An empty string matches everywhere, so this '
                  'would edit the top of the document rather than where you meant.',
                  file=sys.stderr)
            return 2

    root = document(a.src)
    ed = Editor(root, a.author)
    problems = []
    for old, new in a.replace:
        why = ed.apply(old, new, occurrence=a.occurrence)
        print(f'  {"ok  " if not why else "FAIL"}  replace {old[:48]!r}' + (f'  <- {why}' if why else ''))
        if why:
            problems.append(why)
    for anchor, text in a.insert_after:
        why = ed.apply(anchor, text, insert_only=True, occurrence=a.occurrence)
        print(f'  {"ok  " if not why else "FAIL"}  insert after {anchor[:40]!r}' + (f'  <- {why}' if why else ''))
        if why:
            problems.append(why)
    for anchor in a.delete_paragraph:
        why = ed.delete_paragraph(anchor)
        print(f'  {"ok  " if not why else "FAIL"}  delete paragraph {anchor[:40]!r}'
              + (f'  <- {why}' if why else ''))
        if why:
            problems.append(why)
    for anchor, text in a.new_paragraph_after:
        why = ed.new_paragraph(anchor, text)
        print(f'  {"ok  " if not why else "FAIL"}  new paragraph after {anchor[:36]!r}'
              + (f'  <- {why}' if why else ''))
        if why:
            problems.append(why)

    if problems:
        print(f'\n{len(problems)} edit(s) refused; nothing written. '
              'Fix the match text, or leave the protected content alone.')
        return 1
    rewrite(a.src, a.dst, root)
    print(f'\nwrote {a.dst}. Now run verify_edit.py before pushing.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
