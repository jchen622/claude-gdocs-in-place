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
import difflib
import re
import sys

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))  # keep `python3 -I` working

from ooxml import document, paragraphs, paragraphs_spaced, comments, anchor_spans, tracked

# Damage that checks 1 to 4 cannot see, because it disturbs nothing structural: the edit lands
# where it should and still produces broken prose. Every one of these was observed on a real
# manuscript, in a single afternoon, and every one passed the gate as it stood.
ANOMALIES = [
    ('missing space after a full stop', re.compile(r'[a-z]\.[A-Z]')),       # older.A postmarketing
    ('double space',                    re.compile(r'\S {2,}\S')),
    ('doubled word',                    re.compile(r'\b(\w{3,}) \1\b', re.I)),
    # Both halves are real defects (`trial(2)`, `(20)y`). They only look like noise because
    # `24(14)` is ordinary volume(issue) formatting, so reference-list entries are excluded
    # from this scan rather than the rules being dropped. Dropping `\w\(\d` once already let
    # `trial(2)` reach the manuscript.
    ('citation welded to a word',       re.compile(r'[A-Za-z]\(\d|\)[A-Za-z]')),
]


def _counts(text: str) -> dict:
    return {label: len(rx.findall(text)) for label, rx in ANOMALIES}


def _citations(text: str) -> set:
    out = set()
    for m in re.finditer(r'\((\d{1,2}(?:\s*,\s*\d{1,2})*)\)', text):
        out.update(int(n) for n in re.split(r'\s*,\s*', m.group(1)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('before')
    ap.add_argument('after')
    ap.add_argument('--expect-changed', type=int, default=None,
                    help='expected number of changed paragraphs in the accept view')
    ap.add_argument('--allow-dropped-citation', action='append', type=int, default=[],
                    metavar='N',
                    help='acknowledge that reference N is deliberately removed from a paragraph. '
                         'Only silences the citation check for that number, and only when the '
                         'reference is still cited somewhere else in the document, so a genuine '
                         'orphan still fails.')
    ap.add_argument('--expect-text', action='append', default=[],
                    help='string that must appear verbatim in the resulting accept view; '
                         'repeatable. Use one per edit so a replacement that lands in the '
                         'wrong place fails loudly instead of producing plausible wrong text.')
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
    # Align by content so an inserted or removed paragraph does not make every later one look
    # changed. `pairs` holds only genuinely replaced paragraphs; added/removed count separately.
    pairs, added, removed = [], 0, 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ab, aa).get_opcodes():
        if tag == 'replace':
            for x, y in zip(ab[i1:i2], aa[j1:j2]):
                pairs.append((x, y))
            added += max(0, (j2 - j1) - (i2 - i1))
            removed += max(0, (i2 - i1) - (j2 - j1))
        elif tag == 'insert':
            added += j2 - j1
        elif tag == 'delete':
            removed += i2 - i1
    changed = len(pairs) + added + removed
    if a.expect_changed is None:
        print(f'  note  accept view: {changed} paragraph(s) changed')
    elif changed == a.expect_changed:
        print(f'  ok    accept view: {changed} paragraph(s) changed, as expected')
    else:
        fails.append(f'expected {a.expect_changed} changed paragraphs, got {changed}')
        print(f'  FAIL  accept view: {changed} changed, expected {a.expect_changed}')

    # 5. text anomalies newly introduced. Counted, not merely detected: a document already
    #    containing a double space must not make every later edit fail.
    # Spacing-preserving views, with reference-list entries removed: a numbered citation
    # legitimately contains `24(14)` and two spaces after its number.
    REF = re.compile(r'^\s*\d{1,3}\.\s')
    sb = [t for t in paragraphs_spaced(rb, 'accept') if not REF.match(t)]
    sa = [t for t in paragraphs_spaced(ra, 'accept') if not REF.match(t)]
    # Join on newline, not space: a paragraph ending in a trailing space followed by a
    # space separator fabricates a double space at every paragraph boundary, which turned
    # 4 real hits into 22.
    tb_txt, ta_txt = '\n'.join(sb), '\n'.join(sa)
    before_n, after_n = _counts(tb_txt), _counts(ta_txt)
    worse = {k: (before_n[k], after_n[k]) for k in before_n if after_n[k] > before_n[k]}
    if worse:
        fails.append('introduced text anomalies')
        print('  FAIL  the edit introduced broken text')
        for k, (b, c) in worse.items():
            print(f'          {k}: {b} -> {c}')
            rx = dict(ANOMALIES)[k]
            for x, y in zip(sb, sa) if len(sb) == len(sa) else pairs:
                if len(rx.findall(y)) > len(rx.findall(x)):
                    m = rx.search(y)
                    print(f'            …{y[max(0, m.start()-60):m.end()+45]}…')
                    break
    else:
        print('  ok    no new text anomalies (spacing, doubled words, welded citations)')

    # 5b. words fused or truncated by an edit (`receptorswithout`, `dehydroe`). These match no
    #     punctuation pattern, so they are found by looking for alphabetic tokens that are new
    #     to the document and absent from the system word list. Advisory, not a failure: the
    #     word list has no plurals and no technical vocabulary, so a hard fail here would block
    #     every edit that introduces a drug name.
    suspect = []
    try:
        wl = _pl.Path('/usr/share/dict/words')
        words = {w.strip().lower() for w in wl.read_text(errors='replace').splitlines()} if wl.exists() else set()
    except Exception:
        words = set()
    if words:
        tok = lambda s: set(re.findall(r"[A-Za-z]{6,}", s))

        def known(w: str) -> bool:
            # the word list is singular-only and has no inflections, so try the obvious stems
            cands = {w, w.rstrip('.')}
            if w.endswith('ies'):
                cands.add(w[:-3] + 'y')
            for suf in ('s', 'es', 'ed', 'ing', 'd'):
                if w.endswith(suf):
                    cands.add(w[:-len(suf)])
                    cands.add(w[:-len(suf)] + 'e')
            return any(c in words for c in cands)

        for t in sorted(tok(ta_txt) - tok(tb_txt)):
            if not known(t.lower()):
                suspect.append(t)
    if suspect:
        print(f'  WARN  {len(suspect)} new word(s) not in the system word list, check for a '
              f'missing space or a truncation:')
        for t in suspect[:8]:
            m = re.search(r'.{0,45}' + re.escape(t) + r'.{0,35}', ta_txt)
            print(f'          {t}   …{m.group(0) if m else ""}…')

    # 6. a changed paragraph must not silently lose a citation it carried
    still_cited = _citations(' '.join(aa))      # anywhere in the document, after the edit
    lost_cites = []
    for x, y in pairs:
        gone = _citations(x) - _citations(y)
        # An acknowledged drop is only forgiven while the reference survives elsewhere; the
        # moment it becomes an orphan the check fires again regardless of the flag.
        gone -= {n for n in a.allow_dropped_citation if n in still_cited}
        if gone:
            lost_cites.append((sorted(gone), y[:90]))
    if lost_cites:
        fails.append('a changed paragraph lost a citation')
        print('  FAIL  citations dropped by the edit')
        for refs, ctx in lost_cites[:4]:
            print(f'          lost {refs} from: {ctx}…')
    else:
        print('  ok    no citations dropped from changed paragraphs')

    # 7. each edit asserts its own intended output
    missing = [s for s in a.expect_text if s not in ta_txt]
    if a.expect_text:
        if missing:
            fails.append('expected text absent from the result')
            print('  FAIL  expected text not found in the accept view')
            for s in missing:
                print(f'          {s[:100]!r}')
        else:
            print(f'  ok    all {len(a.expect_text)} expected string(s) present')

    tb, ta = tracked(rb), tracked(ra)
    print(f'  note  tracked changes: ins {tb["ins"]} -> {ta["ins"]}, del {tb["del"]} -> {ta["del"]}')

    if fails:
        print(f'\nDO NOT PUSH. {len(fails)} check(s) failed: ' + '; '.join(fails))
        return 1
    print('\nAll checks passed. Safe to PATCH onto the live file id.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
