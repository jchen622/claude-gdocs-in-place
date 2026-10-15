#!/usr/bin/env python3
"""Gate an edited .docx against the one it was derived from. Run this before every push.

    python3 verify_edit.py BEFORE.docx AFTER.docx [--expect-changed N]

Exits non-zero if anything that should have been preserved was not. The checks, in the order
they matter:

  1. comments        identical **item by item**, not merely the same count. A count match hides
                     one comment being destroyed while another is resurrected.
  2. anchor spans    unchanged. A disturbed anchor silently widens, usually to the whole
                     paragraph, and the reviewer loses the phrase they actually marked.
  3. reject view     byte-identical. This is the strongest single check: it proves the edit only
                     *proposed* changes and did not alter settled text. If it fails, whoever
                     rejects your change will not get back what they had.
  4. accept view     exactly the expected number of paragraphs changed, so an edit intended to
                     touch one sentence has not quietly rewritten five.

A clean run means the edit is safe to PATCH onto the live file id.
"""
from __future__ import annotations

import argparse
import sys

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))  # keep `python3 -I` working

from ooxml import document, paragraphs, comments, anchor_spans, tracked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('before')
    ap.add_argument('after')
    ap.add_argument('--expect-changed', type=int, default=None,
                    help='expected number of changed paragraphs in the accept view')
    a = ap.parse_args()

    rb, ra = document(a.before), document(a.after)
    fails = []

    cb, ca = comments(a.before), comments(a.after)
    if cb == ca:
        print(f'  ok    comments identical item by item ({len(ca)})')
    else:
        fails.append('comment set changed')
        lost = [c for c in cb if c not in ca]
        gained = [c for c in ca if c not in cb]
        print(f'  FAIL  comments {len(cb)} -> {len(ca)}')
        for who, txt in lost[:5]:
            print(f'          LOST     [{who}] {txt[:90]}')
        for who, txt in gained[:5]:
            print(f'          APPEARED [{who}] {txt[:90]}')

    sb = sorted(v for v in anchor_spans(rb).values() if v)
    sa = sorted(v for v in anchor_spans(ra).values() if v)
    if sb == sa:
        print(f'  ok    comment anchor spans unchanged ({len(sa)})')
    else:
        fails.append('anchor spans changed')
        print('  FAIL  comment anchors moved or widened')
        for x in [s for s in sb if s not in sa][:5]:
            print(f'          was anchored on: {x[:80]!r}')

    jb, ja = paragraphs(rb, 'reject'), paragraphs(ra, 'reject')
    if jb == ja:
        print('  ok    reject view byte-identical (settled text untouched)')
    else:
        fails.append('reject view changed')
        print(f'  FAIL  reject view differs in {sum(1 for x, y in zip(jb, ja) if x != y)} paragraph(s)')
        for x, y in zip(jb, ja):
            if x != y:
                print(f'          before: {x[:110]}')
                print(f'          after : {y[:110]}')
                break

    ab, aa = paragraphs(rb, 'accept'), paragraphs(ra, 'accept')
    changed = sum(1 for x, y in zip(ab, aa) if x != y) + abs(len(ab) - len(aa))
    if a.expect_changed is None:
        print(f'  note  accept view: {changed} paragraph(s) changed')
    elif changed == a.expect_changed:
        print(f'  ok    accept view: {changed} paragraph(s) changed, as expected')
    else:
        fails.append(f'expected {a.expect_changed} changed paragraphs, got {changed}')
        print(f'  FAIL  accept view: {changed} changed, expected {a.expect_changed}')

    tb, ta = tracked(rb), tracked(ra)
    print(f'  note  tracked changes: ins {tb["ins"]} -> {ta["ins"]}, del {tb["del"]} -> {ta["del"]}')

    if fails:
        print(f'\nDO NOT PUSH. {len(fails)} check(s) failed: ' + '; '.join(fails))
        return 1
    print('\nAll checks passed. Safe to PATCH onto the live file id.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
