#!/usr/bin/env python3
"""Build a minimal .docx carrying comments, for testing resolve behaviour on Drive.

Deliberately hand-rolled OOXML rather than a copy of any real document: the test file gets
uploaded to Drive and poked at, so it must contain nothing confidential.

  python3 -I make_docx.py out.docx [--done 2]

--done marks the Nth comment (1-based) resolved via w15:done="1", to test whether Google
honours the flag the format provides.
"""
from __future__ import annotations

import sys
import zipfile

COMMENTS = [
    ('Alice Tester', 'First comment: this one stays open.', 'alpha'),
    ('Bob Tester',   'Second comment: this one gets resolved.', 'bravo'),
    ('Alice Tester', 'Third comment: this one also stays open.', 'charlie'),
]
BODY_WORDS = ['alpha', 'bravo', 'charlie']

CT = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
<Override PartName="/word/commentsExtended.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"/>
</Types>'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

DOC_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
<Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2011/relationships/commentsExtended" Target="commentsExtended.xml"/>
</Relationships>'''

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
      'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
      'mc:Ignorable="w14 w15" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"')


def build(path: str, done_index: int | None = None) -> None:
    paras = ['<w:p><w:r><w:t xml:space="preserve">'
             'Test document for Google Drive comment-resolution behaviour. '
             'Each keyword below carries one comment.</w:t></w:r></w:p>']
    for i, (_, _, word) in enumerate(COMMENTS):
        paras.append(
            f'<w:p>'
            f'<w:r><w:t xml:space="preserve">Paragraph {i+1} mentions </w:t></w:r>'
            f'<w:commentRangeStart w:id="{i}"/>'
            f'<w:r><w:t>{word}</w:t></w:r>'
            f'<w:commentRangeEnd w:id="{i}"/>'
            f'<w:r><w:commentReference w:id="{i}"/></w:r>'
            f'<w:r><w:t xml:space="preserve"> and then continues to the end of the line.'
            f'</w:t></w:r>'
            f'</w:p>')
    # A tracked insertion and a tracked deletion, to test what conversion does to review markup.
    paras.append(
        '<w:p><w:r><w:t xml:space="preserve">Tracked markup follows: </w:t></w:r>'
        '<w:ins w:id="900" w:author="Carol Reviewer" w:date="2026-10-17T12:00:00Z">'
        '<w:r><w:t xml:space="preserve">this phrase was inserted</w:t></w:r></w:ins>'
        '<w:r><w:t xml:space="preserve"> and </w:t></w:r>'
        '<w:del w:id="901" w:author="Carol Reviewer" w:date="2026-10-17T12:00:00Z">'
        '<w:r><w:delText xml:space="preserve">this phrase was deleted</w:delText></w:r></w:del>'
        '<w:r><w:t xml:space="preserve"> at the end.</w:t></w:r></w:p>')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document {NS}><w:body>{"".join(paras)}'
                f'<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
                f'</w:body></w:document>')

    cs, exs = [], []
    for i, (author, text, _) in enumerate(COMMENTS):
        pid = f'{i+1:08X}'
        cs.append(f'<w:comment w:id="{i}" w:author="{author}" w:initials="{author[0]}" '
                  f'w:date="2026-10-17T12:00:00Z">'
                  f'<w:p w14:paraId="{pid}"><w:r><w:t>{text}</w:t></w:r></w:p></w:comment>')
        flag = '1' if (done_index is not None and i == done_index - 1) else '0'
        exs.append(f'<w15:commentEx w15:paraId="{pid}" w15:done="{flag}"/>')
    comments = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:comments {NS}>{"".join(cs)}</w:comments>')
    extended = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w15:commentsEx {NS}>{"".join(exs)}</w15:commentsEx>')

    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', CT)
        z.writestr('_rels/.rels', RELS)
        z.writestr('word/_rels/document.xml.rels', DOC_RELS)
        z.writestr('word/document.xml', document)
        z.writestr('word/comments.xml', comments)
        z.writestr('word/commentsExtended.xml', extended)
    print(f'wrote {path}: {len(COMMENTS)} comments'
          + (f', #{done_index} marked done="1"' if done_index else ''))


if __name__ == '__main__':
    out = sys.argv[1]
    di = int(sys.argv[sys.argv.index('--done') + 1]) if '--done' in sys.argv else None
    build(out, di)
