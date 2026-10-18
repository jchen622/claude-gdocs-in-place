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

    def apply(self, old: str, new: str, *, insert_only=False) -> str:
        """Returns '' on success or a reason string on refusal."""
        for para in self.root.iter(W + 'p'):
            guard = _protected(para)
            for child in list(para):
                if child.tag != W + 'r' or id(child) in guard:
                    continue
                t = child.find(W + 't')
                if t is None or old not in (t.text or ''):
                    continue
                # Anchors are checked here, against this paragraph only. Checking against every
                # anchor in the document refuses edits that overlap nothing: an anchor on a short
                # identifier in one paragraph would block that same string everywhere else.
                # Refuse outright rather than trying the next paragraph, so an anchored first
                # occurrence can never silently redirect the edit to a different occurrence.
                for a in self.para_anchors.get(id(para), []):
                    if len(a) > 3 and (old in a or a in old):
                        return f'overlaps a comment anchor in this paragraph: {a[:60]!r}'
                txt = t.text
                i = txt.index(old)
                head, tail = txt[:i], txt[i + len(old):]
                rpr = child.find(W + 'rPr')
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
        return f'no unprotected run contains {old[:60]!r}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--author', required=True)
    ap.add_argument('--replace', nargs=2, action='append', metavar=('OLD', 'NEW'), default=[])
    ap.add_argument('--insert-after', nargs=2, action='append',
                    metavar=('ANCHOR', 'TEXT'), default=[])
    ap.add_argument('--new-paragraph-after', nargs=2, action='append',
                    metavar=('ANCHOR', 'TEXT'), default=[],
                    help='add a whole new paragraph after the one containing ANCHOR, copying '
                         'its paragraph properties. Wrap italic spans in *asterisks*. Needed '
                         'for things like a reference-list entry, which --insert-after cannot '
                         'do because it can only add text inside an existing paragraph.')
    a = ap.parse_args()

    root = document(a.src)
    ed = Editor(root, a.author)
    problems = []
    for old, new in a.replace:
        why = ed.apply(old, new)
        print(f'  {"ok  " if not why else "FAIL"}  replace {old[:48]!r}' + (f'  <- {why}' if why else ''))
        if why:
            problems.append(why)
    for anchor, text in a.insert_after:
        why = ed.apply(anchor, text, insert_only=True)
        print(f'  {"ok  " if not why else "FAIL"}  insert after {anchor[:40]!r}' + (f'  <- {why}' if why else ''))
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
