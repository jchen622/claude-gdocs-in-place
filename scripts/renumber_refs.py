#!/usr/bin/env python3
"""Renumber a .docx reference list into first-citation order, and remap every in-text citation.

    python3 renumber_refs.py IN.docx OUT.docx [--drop-uncited] [--report]
    python3 renumber_refs.py --check IN.docx OUT.docx

`tracked_edit.py` cannot do this job. It matches one unique string at a time, and `(3)` occurs
many times; worse, renaming sequentially aliases, because rewriting 3 to 2 and then 2 to 3
corrupts both. This rewrites every citation in a single simultaneous pass instead.

**This lands untracked, on purpose.** On a real manuscript the change is ~114 citation sites plus
the whole reference list; as tracked changes that is unreadable and roughly doubles the document
XML. Renumbering is mechanical housekeeping, not a content change anyone reviews. The price is
that `verify_edit.py`'s reject-view check cannot apply, so `--check` here replaces it with a
stronger, semantic gate: the claim-to-source mapping and the DOI set, not the byte image.

Two structural facts make the rewrite safe, and `--check` re-proves both on the result:

  * a citation must sit wholly inside one `<w:t>`. If any span run boundaries the run refuses,
    rather than silently rewriting half of one.
  * the reference entries must be contiguous siblings, so reordering moves whole `<w:p>` elements
    and entries carrying a `<w:hyperlink>` keep their links and their formatting.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET

import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))  # keep `python3 -I` working

from ooxml import W, document, paragraphs, comments, anchor_spans, tracked, rewrite

ET.register_namespace('w', 'http://schemas.openxmlformats.org/wordprocessingml/2006/main')

ENTRY = re.compile(r'^\s*(\d{1,3})\.\s+\S')
CIT = re.compile(r'\((\d{1,3}(?:\s*,\s*\d{1,3})*)\)')
DOI = re.compile(r'10\.\d{4,9}/[^\s,;]+')


def _text(el) -> str:
    return ''.join(t.text or '' for t in el.iter(W + 't'))


def ref_block(root) -> tuple[list, object, int]:
    """The reference-entry paragraphs, their shared parent, and the index of the first.

    An entry is a numbered paragraph long enough not to be a numbered *heading*; section headings
    like `4.1 Intravenous pharmacokinetics` are short and must not be swept in.
    """
    parents = {id(c): p for p in root.iter() for c in p}
    hits = [p for p in root.iter(W + 'p')
            if ENTRY.match(_text(p).strip()) and len(_text(p)) > 60]
    if not hits:
        raise SystemExit('no reference entries found')
    parent = parents[id(hits[0])]
    idx = [list(parent).index(p) for p in hits]
    if idx != list(range(idx[0], idx[0] + len(idx))):
        raise SystemExit('reference entries are not contiguous siblings; reorder by hand')
    return hits, parent, idx[0]


def cite_order(root, refnums: set) -> list[int]:
    """Reference numbers in order of first citation, scanning document order and skipping the
    reference list itself (a journal entry prints `24(14)`, which is a volume and issue)."""
    out, seen = [], set()
    for p in root.iter(W + 'p'):
        t = _text(p)
        if ENTRY.match(t.strip()) and len(t) > 60:
            continue
        for m in CIT.finditer(t):
            for n in (int(x) for x in re.split(r'\s*,\s*', m.group(1))):
                if n in refnums and n not in seen:
                    seen.add(n)
                    out.append(n)
    return out


def build_map(root, drop_uncited: bool) -> tuple[dict, list[int]]:
    entries, _, _ = ref_block(root)
    nums = {int(ENTRY.match(_text(p).strip()).group(1)): p for p in entries}
    order = cite_order(root, set(nums))
    uncited = sorted(set(nums) - set(order))
    if uncited and not drop_uncited:
        raise SystemExit(f'references {uncited} are cited nowhere. Cite them, or pass '
                         f'--drop-uncited to remove them.')
    return {old: i for i, old in enumerate(order, 1)}, uncited


def remap_citations(root, mapping: dict) -> int:
    """Rewrite every citation outside the reference list, simultaneously."""
    def sub(m):
        nums = [int(x) for x in re.split(r'\s*,\s*', m.group(1))]
        if any(n not in mapping for n in nums):
            return m.group(0)          # not reference numbers; leave untouched
        return '(' + ','.join(str(mapping[n]) for n in nums) + ')'

    n = 0
    for p in root.iter(W + 'p'):
        whole = _text(p)
        if ENTRY.match(whole.strip()) and len(whole) > 60:
            continue
        # refuse rather than half-rewrite a citation that straddles two runs
        runs = [t for t in p.iter(W + 't')]
        pos, spans = 0, []
        for t in runs:
            spans.append((pos, pos + len(t.text or '')))
            pos += len(t.text or '')
        for m in CIT.finditer(whole):
            if not any(a <= m.start() and m.end() <= b for a, b in spans):
                raise SystemExit(f'citation {m.group(0)} spans a run boundary in: '
                                 f'{whole[:80]!r}. Fix by hand.')
        for t in runs:
            new = CIT.sub(sub, t.text or '')
            if new != (t.text or ''):
                n += 1
                t.text = new
    return n


def apply(root, mapping: dict, uncited: list[int]) -> None:
    entries, parent, start = ref_block(root)
    by_num = {int(ENTRY.match(_text(p).strip()).group(1)): p for p in entries}
    remap_citations(root, mapping)
    for p in entries:
        parent.remove(p)
    for old, new in sorted(mapping.items(), key=lambda kv: kv[1]):
        p = by_num[old]
        first = next(iter(p.iter(W + 't')), None)
        if first is not None:                      # keep the spacing that follows the number
            first.text = re.sub(r'^\s*\d{1,3}\.', f'{new}.', first.text or '', count=1)
        parent.insert(start + new - 1, p)
    # uncited entries are simply not re-inserted


def check(before: str, after: str) -> int:
    rb, ra = document(before), document(after)
    mapping, uncited = build_map(rb, drop_uncited=True)
    inverse = {v: k for k, v in mapping.items()}
    fails = []

    eb, _, _ = ref_block(rb)
    ea, _, _ = ref_block(ra)
    old_txt = {int(ENTRY.match(_text(p).strip()).group(1)): _text(p) for p in eb}
    new_txt = {int(ENTRY.match(_text(p).strip()).group(1)): _text(p) for p in ea}

    # 1. nothing changed but the numbers
    strip = lambda s: CIT.sub('(#)', s)
    bb = [strip(t) for t in paragraphs(rb, 'accept') if not (ENTRY.match(t) and len(t) > 60)]
    aa = [strip(t) for t in paragraphs(ra, 'accept') if not (ENTRY.match(t) and len(t) > 60)]
    if bb == aa:
        print(f'  ok    body text identical once citation numbers are masked ({len(aa)} paragraphs)')
    else:
        fails.append('body text changed beyond the numbers')
        print(f'  FAIL  body text differs in {sum(1 for x, y in zip(bb, aa) if x != y)} paragraph(s)')
        for x, y in zip(bb, aa):
            if x != y:
                print(f'          before: {x[:110]}\n          after : {y[:110]}')
                break

    # 2. claim-to-source identity, position by position
    cb = [[int(x) for x in re.split(r'\s*,\s*', m.group(1))]
          for t in paragraphs(rb, 'accept') if not (ENTRY.match(t) and len(t) > 60)
          for m in CIT.finditer(t)]
    ca = [[int(x) for x in re.split(r'\s*,\s*', m.group(1))]
          for t in paragraphs(ra, 'accept') if not (ENTRY.match(t) and len(t) > 60)
          for m in CIT.finditer(t)]
    want = [[mapping.get(n, n) for n in g] for g in cb]
    if want == ca:
        print(f'  ok    all {len(ca)} citation sites point at the same references as before')
    else:
        fails.append('a citation site changed meaning')
        print('  FAIL  citation sites do not map through')
        for i, (w_, a_) in enumerate(zip(want, ca)):
            if w_ != a_:
                print(f'          site {i}: expected {w_}, got {a_}')
                break

    # 3. the DOI set, which is what catches a reordering break. Never compare by position.
    db = {d for n, t in old_txt.items() if n not in uncited for d in DOI.findall(t)}
    da = {d for t in new_txt.values() for d in DOI.findall(t)}
    if db == da:
        print(f'  ok    DOI set identical ({len(da)} DOIs)')
    else:
        fails.append('DOI set changed')
        print(f'  FAIL  DOI set changed: lost {sorted(db - da)[:4]}, gained {sorted(da - db)[:4]}')

    # 3b. each new number carries the entry the map says it should
    bad = [n for n in new_txt
           if re.sub(r'^\s*\d{1,3}\.', '', new_txt[n]).strip()
           != re.sub(r'^\s*\d{1,3}\.', '', old_txt[inverse[n]]).strip()]
    if bad:
        fails.append('an entry landed under the wrong number')
        print(f'  FAIL  entries under the wrong number: {bad[:6]}')
    else:
        print(f'  ok    every new number carries the entry it should ({len(new_txt)})')

    # 4. well formed, and genuinely in first-citation order now
    want_nums = list(range(1, len(new_txt) + 1))
    if sorted(new_txt) != want_nums:
        fails.append('reference numbering has gaps')
        print(f'  FAIL  numbering is {sorted(new_txt)[:8]}…, expected 1..{len(new_txt)}')
    elif cite_order(ra, set(new_txt)) != want_nums:
        fails.append('result is still not in first-citation order')
        got = cite_order(ra, set(new_txt))
        print(f'  FAIL  first-citation order is {got[:10]}…')
    else:
        print(f'  ok    entries 1 to {len(new_txt)}, no gaps, in first-citation order')

    # 5. dropped entries named in full
    for n in uncited:
        print(f'  note  DROPPED (cited nowhere) old {n}: {old_txt[n][:150]}')

    # 6. everything around it untouched
    for label, x, y in (('comments', comments(before), comments(after)),
                        ('anchor spans', sorted(anchor_spans(rb).values()),
                         sorted(anchor_spans(ra).values())),
                        ('tracked changes', tracked(rb), tracked(ra))):
        if x == y:
            print(f'  ok    {label} unchanged')
        else:
            fails.append(f'{label} changed')
            print(f'  FAIL  {label} changed: {x} -> {y}')

    import zipfile
    for label, f in (('before', before), ('after', after)):
        z = zipfile.ZipFile(f)
        rels = z.read('word/_rels/document.xml.rels').decode()
        print(f'  note  {label}: {rels.count("relationships/hyperlink")} hyperlink rels, '
              f'{len([n for n in z.namelist() if n.startswith("word/media/")])} images')

    if fails:
        print(f'\nDO NOT PUSH. {len(fails)} check(s) failed: ' + '; '.join(fails))
        return 1
    print('\nAll checks passed. Safe to PATCH onto the live file id.')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='gate a result instead of producing one')
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--drop-uncited', action='store_true',
                    help='remove entries cited nowhere. Without it an uncited entry is an error, '
                         'because silently dropping a reference is how one goes missing.')
    ap.add_argument('--report', action='store_true', help='print the old to new map')
    a = ap.parse_args()

    if a.check:
        return check(a.src, a.dst)

    root = document(a.src)
    mapping, uncited = build_map(root, a.drop_uncited)
    if a.report:
        print('  old -> new: ' + ', '.join(f'{o}->{mapping[o]}' for o in sorted(mapping)))
        print(f'  uncited, dropped: {uncited or "none"}')
        print(f'  unchanged numbers: {sorted(o for o in mapping if mapping[o] == o)}')
    apply(root, mapping, uncited)
    rewrite(a.src, a.dst, root)
    print(f'\nwrote {a.dst}: {len(mapping)} references renumbered, {len(uncited)} dropped.')
    print(f'Now run: {_pl.Path(__file__).name} --check {a.src} {a.dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
