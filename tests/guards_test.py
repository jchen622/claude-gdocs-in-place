#!/usr/bin/env python3
"""Regression suite for the refusals and the round-trip.

    python3 -I guards_test.py [-v]

Every case here is a defect that shipped, or a negative control proving a guard has not been
tuned into uselessness. Each was written to fail before its fix and pass after. Nothing here
touches the network: fixtures are built in a temp directory and thrown away.

The namespace case is the one that matters most. It is invisible to every text-level check in
verify_edit.py, so without a test the only thing standing between a corrupt .docx and the
author's live file is somebody opening it by hand.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))
sys.path.insert(0, str(HERE))

from ooxml import W, document, rewrite, root_declarations, paragraphs_spaced  # noqa: E402
from tracked_edit import Editor, _scan, _unsplittable  # noqa: E402

PASS, FAIL = [], []
VERBOSE = '-v' in sys.argv


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    if VERBOSE or not cond:
        print(f'  {"ok  " if cond else "FAIL"}  {name}' + (f'  <- {detail}' if detail and not cond else ''))


# --------------------------------------------------------------------------------------------
# Fixture: a minimal document.xml with the namespace shape every real Docs/Word export has.
# --------------------------------------------------------------------------------------------
NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
      'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
      'mc:Ignorable="w14 w15"')

CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="xml" ContentType="application/xml"/>'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
      'officedocument.wordprocessingml.document.main+xml"/></Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def docx(tmp: pathlib.Path, body: str, name='t.docx') -> str:
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document {NS}><w:body>{body}</w:body></w:document>')
    p = tmp / name
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('[Content_Types].xml', CT)
        z.writestr('_rels/.rels', RELS)
        z.writestr('word/document.xml', xml)
    return str(p)


def para(*runs):
    return '<w:p>' + ''.join(runs) + '</w:p>'


def run(text, extra=''):
    return f'<w:r><w:rPr><w:b/></w:rPr>{extra}<w:t xml:space="preserve">{text}</w:t></w:r>'


def edited(tmp, src, fn, out='out.docx'):
    """Run `fn(editor)` and rewrite to `out`. Returns (reason, out_path)."""
    root = document(src)
    ed = Editor(root, 'Test Author')
    why = fn(ed)
    dst = str(tmp / out)
    if not why:
        rewrite(src, dst, root)
    return why, dst


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='gdip-test-'))
    try:
        # --- 1. namespace prefixes survive the round trip (reviewer reproduced this) ---------
        src = docx(tmp, para(run('Hello world')))
        _, out = edited(tmp, src, lambda e: e.apply('Hello', 'Goodbye'))
        xml = zipfile.ZipFile(out).read('word/document.xml')
        head = re.search(rb'<[A-Za-z_][^>]*>', xml).group(0).decode()

        check('1a namespace prefixes preserved, not ns0/ns1',
              'ns0:' not in head and '<w:document' in head, head[:160])

        decls = root_declarations(xml)
        ign = re.search(r'mc:Ignorable="([^"]*)"', head)
        named = ign.group(1).split() if ign else []
        check('1b mc:Ignorable survives', bool(named), head[:160])
        check('1c every prefix mc:Ignorable names is declared',
              all(p in decls for p in named),
              f'named={named} declared={sorted(decls)}')
        check('1d output still parses', ET.fromstring(xml) is not None)

        # Negative control: the guard must actually be doing something. A document with no
        # extra prefixes should round-trip too, but 1c must not pass merely by finding nothing.
        check('1e negative control: Ignorable really did name prefixes', named == ['w14', 'w15'])

        # --- 2. rewrite refuses to write over its own source --------------------------------
        same = docx(tmp, para(run('Alpha')), name='same.docx')
        try:
            rewrite(same, same, document(same))
            check('2a rewrite(src, src) refused', False, 'no exception raised')
        except ValueError:
            check('2a rewrite(src, src) refused', True)
        check('2b source survived the refusal', zipfile.ZipFile(same).read('word/document.xml') != b'')

        # --- 3. splitting a run must not drop its other children ----------------------------
        src = docx(tmp, para('<w:r><w:t xml:space="preserve">See</w:t><w:tab/>'
                             '<w:t xml:space="preserve">Table 1</w:t></w:r>'))
        why, out = edited(tmp, src, lambda e: e.apply('See', 'Refer to'), out='tab.docx')
        check('3a run holding a tab + two texts is refused, not silently gutted',
              bool(why) and 'without losing content' in why, why or 'edit was allowed')
        check('3b _unsplittable names the tab', 'w:tab' in _unsplittable(
            ET.fromstring(f'<w:r {NS}><w:t>See</w:t><w:tab/></w:r>')))
        check('3c negative control: a plain single-text run is splittable',
              _unsplittable(ET.fromstring(f'<w:r {NS}><w:rPr/><w:t>See</w:t></w:r>')) == '')

        # --- 4. empty OLD is refused --------------------------------------------------------
        src = docx(tmp, para(run('Alpha beta')))
        why, _ = edited(tmp, src, lambda e: e.apply('', 'Draft: '), out='empty.docx')
        check('4a empty OLD refused', bool(why) and 'empty' in why.lower(), why or 'allowed')

        # --- 5. two matches are refused unless an occurrence is named -----------------------
        src = docx(tmp, para(run('alpha')) + para(run('alpha')))
        why, _ = edited(tmp, src, lambda e: e.apply('alpha', 'omega'), out='two.docx')
        check('5a 2 matches refused without --occurrence',
              bool(why) and 'occurrence' in why, why or 'allowed')
        why, out = edited(tmp, src, lambda e: e.apply('alpha', 'omega', occurrence=2),
                          out='two_ok.docx')
        check('5b --occurrence 2 accepted', not why, why)
        if not why:
            rej = paragraphs_spaced(document(out), 'reject')
            acc = paragraphs_spaced(document(out), 'accept')
            check('5c the second occurrence was the one edited',
                  acc[0] == 'alpha' and acc[1] == 'omega', f'accept={acc}')
            check('5d reject view still both originals', rej == ['alpha', 'alpha'], f'{rej}')
        why, _ = edited(tmp, src, lambda e: e.apply('alpha', 'omega', occurrence=9),
                        out='two_bad.docx')
        check('5e out-of-range occurrence refused', bool(why) and 'out of range' in why, why)

        # --- 6. comment anchors: positional overlap, not string containment -----------------
        # Anchor covers "quick brown"; the edit targets "brown fox". Neither string contains
        # the other, so the old containment check passed this and ate half the anchored text.
        body = ('<w:p><w:commentRangeStart w:id="1"/>'
                '<w:r><w:t xml:space="preserve">quick brown</w:t></w:r>'
                '<w:commentRangeEnd w:id="1"/>'
                '<w:r><w:t xml:space="preserve"> fox jumps</w:t></w:r></w:p>')
        src = docx(tmp, body, name='anchor.docx')
        why, _ = edited(tmp, src, lambda e: e.apply('brown', 'red'), out='anchor_out.docx')
        check('6a partial overlap with an anchor is refused',
              bool(why) and 'anchor' in why, why or 'allowed')
        why, _ = edited(tmp, src, lambda e: e.apply('jumps', 'leaps'), out='anchor_ok.docx')
        check('6b negative control: text outside the anchor is still editable', not why, why)

        # Precision in the other direction. The same word sits inside an anchor and again
        # outside it. Containment refused both, because the search string equalled the anchor
        # text, so a legitimate edit elsewhere in the paragraph was unreachable.
        body = ('<w:p><w:commentRangeStart w:id="1"/>'
                '<w:r><w:t xml:space="preserve">alpha</w:t></w:r>'
                '<w:commentRangeEnd w:id="1"/>'
                '<w:r><w:t xml:space="preserve"> beta alpha</w:t></w:r></w:p>')
        src = docx(tmp, body, name='both.docx')
        why, out = edited(tmp, src, lambda e: e.apply('alpha', 'omega', occurrence=2),
                          out='both_out.docx')
        check('6d the unanchored copy of an anchored word is still editable', not why, why)
        if not why:
            check('6e the anchored copy was left alone',
                  paragraphs_spaced(document(out), 'accept') == ['alpha beta omega'],
                  str(paragraphs_spaced(document(out), 'accept')))

        # Short anchors used to be skipped outright by the `len(a) > 3` test.
        body = ('<w:p><w:commentRangeStart w:id="1"/>'
                '<w:r><w:t xml:space="preserve">pH</w:t></w:r>'
                '<w:commentRangeEnd w:id="1"/></w:p>')
        src = docx(tmp, body, name='short.docx')
        why, _ = edited(tmp, src, lambda e: e.apply('pH', 'pD'), out='short_out.docx')
        check('6c a 2-character anchor is still protected', bool(why) and 'anchor' in why,
              why or 'allowed')

        # --- 7. someone else's pending change is left alone ---------------------------------
        body = ('<w:p><w:ins w:id="5" w:author="Other" w:date="2026-01-01T00:00:00Z">'
                '<w:r><w:t xml:space="preserve">theirs</w:t></w:r></w:ins></w:p>')
        src = docx(tmp, body, name='ins.docx')
        why, _ = edited(tmp, src, lambda e: e.apply('theirs', 'mine'), out='ins_out.docx')
        check('7a text inside another author\'s w:ins is refused', bool(why), why or 'allowed')

        # --- 8. paragraph-mark revisions and block-level sdt --------------------------------
        body = ('<w:p><w:pPr><w:rPr><w:ins w:id="7" w:author="Other" '
                'w:date="2026-01-01T00:00:00Z"/></w:rPr></w:pPr>'
                '<w:r><w:t xml:space="preserve">split here</w:t></w:r></w:p>')
        src = docx(tmp, body, name='pmark.docx')
        why, _ = edited(tmp, src, lambda e: e.apply('split', 'join'), out='pmark_out.docx')
        check('8a paragraph-mark insertion protects the paragraph',
              bool(why) and 'paragraph mark' in why, why or 'allowed')

        body = f'<w:sdt><w:sdtContent>{para(run("wrapped"))}</w:sdtContent></w:sdt>'
        src = docx(tmp, body, name='sdt.docx')
        why, _ = edited(tmp, src, lambda e: e.apply('wrapped', 'changed'), out='sdt_out.docx')
        check('8b block-level w:sdt protects the paragraph inside it',
              bool(why) and 'sdt' in why, why or 'allowed')

        # --- 9. rPrChange is not duplicated across the split fragments ----------------------
        body = ('<w:p><w:r><w:rPr><w:b/><w:rPrChange w:id="9" w:author="Other" '
                'w:date="2026-01-01T00:00:00Z"><w:rPr/></w:rPrChange></w:rPr>'
                '<w:t xml:space="preserve">alpha beta gamma</w:t></w:r></w:p>')
        src = docx(tmp, body, name='rprchange.docx')
        why, out = edited(tmp, src, lambda e: e.apply('beta', 'delta'), out='rpr_out.docx')
        check('9a edit over an rPrChange run succeeded', not why, why)
        if not why:
            n = len(list(document(out).iter(W + 'rPrChange')))
            check('9b rPrChange not copied into every fragment', n == 0,
                  f'{n} copies left after the split')

        # --- 10. the happy path still works -------------------------------------------------
        src = docx(tmp, para(run('The quick brown fox')), name='happy.docx')
        why, out = edited(tmp, src, lambda e: e.apply('quick', 'slow'), out='happy_out.docx')
        check('10a a plain replacement is allowed', not why, why)
        if not why:
            r = document(out)
            check('10b reject view unchanged',
                  paragraphs_spaced(r, 'reject') == ['The quick brown fox'],
                  str(paragraphs_spaced(r, 'reject')))
            check('10c accept view shows the replacement',
                  paragraphs_spaced(r, 'accept') == ['The slow brown fox'],
                  str(paragraphs_spaced(r, 'accept')))
            check('10d bold preserved on every fragment',
                  len(list(r.iter(W + 'b'))) >= 3, f"{len(list(r.iter(W + 'b')))} w:b")

        # --- 11. _scan offsets line up with the anchor ranges -------------------------------
        root = document(docx(tmp, body, name='scan.docx'))
        p = next(root.iter(W + 'p'))
        text, runs, anchors = _scan(p)
        check('11a _scan text matches the raw paragraph text', text == 'alpha beta gamma', text)
        check('11b _scan reports one direct-child run',
              len([r for r in runs if r[3]]) == 1, str(runs))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n' + '=' * 62)
    print(f'passed {len(PASS)}, FAILED {len(FAIL)}')
    for f in FAIL:
        print(f'  - {f}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
