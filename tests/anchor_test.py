#!/usr/bin/env python3
"""Do comment anchors survive .docx -> native Google Doc conversion?

A comment that converts but loses its anchor is worse than it looks: it detaches from the
phrase the reviewer marked and the thread no longer points at anything. The Drive API returning
an empty quotedFileContent is not proof either way, so this checks the exported document.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path.home() / '.claude/skills/gdocs-in-place/scripts'))
from drive_io import API, UPLOAD, DOCX, _request, token, docx_comments  # noqa: E402
from ooxml import document, anchor_spans  # noqa: E402

HERE = pathlib.Path(__file__).parent
GDOC = 'application/vnd.google-apps.document'


def main() -> int:
    subprocess.run([sys.executable, '-I', str(HERE / 'make_docx.py'),
                    str(HERE / 'anch.docx')], check=True)
    src = HERE / 'anch.docx'
    print('BEFORE conversion, anchors in the local .docx:')
    for c in docx_comments(str(src)):
        print(f'   {c["author"]:14} anchor={c["anchor"]!r:14} {c["text"][:40]!r}')

    meta = {'name': 'ZZ_anchor_test', 'mimeType': GDOC}
    b = '===' + uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'
            f'{json.dumps(meta)}\r\n--{b}\r\nContent-Type: {DOCX}\r\n\r\n').encode()
    body += src.read_bytes() + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request(f'{UPLOAD}?uploadType=multipart&fields=id',
                                 data=body, method='POST',
                                 headers={'Authorization': 'Bearer ' + token(),
                                          'Content-Type': f'multipart/related; boundary={b}'})
    fid = json.load(urllib.request.urlopen(req))['id']
    print(f'\nconverted to native Doc {fid}')

    q = urllib.parse.urlencode({'pageSize': '100',
                                'fields': 'comments(author/displayName,content,'
                                          'quotedFileContent/value)'})
    _, cb = _request(f'{API}/{fid}/comments?{q}')
    print('\nAFTER conversion, what the Drive comments API reports:')
    for c in json.loads(cb).get('comments', []):
        print(f'   {(c.get("author") or {}).get("displayName"):14} '
              f'quotedFileContent={(c.get("quotedFileContent") or {}).get("value", "")!r:14} '
              f'{(c.get("content") or "")[:40]!r}')

    _, eb = _request(f'{API}/{fid}/export?mimeType={urllib.parse.quote(DOCX)}')
    out = HERE / '_anchor_back.docx'
    out.write_bytes(eb)
    print('\nAFTER conversion, anchors in the document exported back out:')
    for c in docx_comments(str(out)):
        print(f'   {c["author"]:14} anchor={c["anchor"]!r:14} {c["text"][:40]!r}')
    spans = anchor_spans(document(str(out)))
    kept = sum(1 for v in spans.values() if v.strip())
    print(f'\nanchors with text: {kept} of {len(spans)}')

    _request(f'{API}/{fid}?supportsAllDrives=true', method='DELETE')
    print('trashed the test file')
    return 0


if __name__ == '__main__':
    sys.exit(main())
