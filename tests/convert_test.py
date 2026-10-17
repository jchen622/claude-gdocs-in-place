#!/usr/bin/env python3
"""Can a stored .docx become a native Google Doc, and what survives?

Two questions, in order of value:

  1. Can it convert IN PLACE, keeping the same file id? The id carries comment threads,
     @-mention notifications, the share list and every link already pasted elsewhere, so a
     conversion that mints a new id costs all of that.
  2. What survives either way: open comments, their authors, and tracked changes?

Runs against throwaway files it creates and deletes.

  python3 -I convert_test.py
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
from ooxml import document, paragraphs, tracked  # noqa: E402

HERE = pathlib.Path(__file__).parent
GDOC = 'application/vnd.google-apps.document'
CF = 'comments(id,author/displayName,content,resolved,deleted,quotedFileContent/value,replies(content))'
trash: list[str] = []


def upload(path: pathlib.Path, name: str, convert: bool) -> str:
    meta = {'name': name}
    if convert:
        meta['mimeType'] = GDOC
    b = '===' + uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'
            f'{json.dumps(meta)}\r\n--{b}\r\nContent-Type: {DOCX}\r\n\r\n').encode()
    body += path.read_bytes() + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request(f'{UPLOAD}?uploadType=multipart&fields=id,mimeType',
                                 data=body, method='POST',
                                 headers={'Authorization': 'Bearer ' + token(),
                                          'Content-Type': f'multipart/related; boundary={b}'})
    d = json.load(urllib.request.urlopen(req))
    trash.append(d['id'])
    return d['id']


def comments_of(fid: str) -> list[dict]:
    q = urllib.parse.urlencode({'includeDeleted': 'true', 'pageSize': '100', 'fields': CF})
    _, body = _request(f'{API}/{fid}/comments?{q}')
    return [c for c in json.loads(body).get('comments', []) if not c.get('deleted')]


def report(tag: str, fid: str):
    _, m = _request(f'{API}/{fid}?fields=id,mimeType,name')
    meta = json.loads(m)
    print(f'\n--- {tag}')
    print(f'    id       : {fid}')
    print(f'    mimeType : {meta["mimeType"]}')
    cs = comments_of(fid)
    print(f'    comments : {len(cs)}')
    for c in cs:
        print(f'       {(c.get("author") or {}).get("displayName")!r}: '
              f'{(c.get("content") or "")[:44]!r} resolved={c.get("resolved")} '
              f'anchor={(c.get("quotedFileContent") or {}).get("value","")[:22]!r}')
    if meta['mimeType'] == GDOC:
        _, b = _request(f'{API}/{fid}/export?mimeType={urllib.parse.quote(DOCX)}')
        p = HERE / f'_conv_{fid[:8]}.docx'
        p.write_bytes(b)
        root = document(str(p))
        print(f'    exported back to .docx: tracked={tracked(root)}, '
              f'comments-in-file={len(docx_comments(str(p)))}')
        txt = ' '.join(paragraphs(root, "accept"))
        print(f'    inserted phrase present : {"this phrase was inserted" in txt}')
        print(f'    deleted phrase in reject: '
              f'{"this phrase was deleted" in " ".join(paragraphs(root, "reject"))}')


def main() -> int:
    subprocess.run([sys.executable, '-I', str(HERE / 'make_docx.py'),
                    str(HERE / 'conv.docx')], check=True)
    src = HERE / 'conv.docx'
    root = document(str(src))
    print(f'local source: {len(docx_comments(str(src)))} comments, tracked={tracked(root)}')

    a = upload(src, 'ZZ_convert_stored', convert=False)
    report('uploaded as a stored .docx', a)

    print('\n=== Q1: convert IN PLACE on the same id?')
    for how, fn in (
        ('PATCH metadata mimeType',
         lambda: _request(f'{API}/{a}?fields=id,mimeType', method='PATCH',
                          data=json.dumps({'mimeType': GDOC}).encode(),
                          content_type='application/json')),
        ('PATCH media upload with mimeType',
         lambda: _request(f'{UPLOAD}/{a}?uploadType=media&fields=id,mimeType',
                          method='PATCH', data=src.read_bytes(), content_type=GDOC)),
    ):
        try:
            _, r = fn()
            print(f'    {how}: {json.loads(r)}')
        except SystemExit as e:
            print(f'    {how}: REFUSED {str(e)[:150]}')
    _, m = _request(f'{API}/{a}?fields=mimeType')
    print(f'    mimeType now: {json.loads(m)["mimeType"]}')

    print('\n=== Q2: convert by copy (new id) and by fresh upload')
    try:
        _, r = _request(f'{API}/{a}/copy?fields=id,mimeType', method='POST',
                        data=json.dumps({'mimeType': GDOC,
                                         'name': 'ZZ_convert_copy'}).encode(),
                        content_type='application/json')
        cid = json.loads(r)['id']
        trash.append(cid)
        report('files.copy with conversion', cid)
    except SystemExit as e:
        print('    copy failed:', str(e)[:160])

    b = upload(src, 'ZZ_convert_fresh_native', convert=True)
    report('fresh upload converted at ingest', b)

    for fid in trash:
        try:
            _request(f'{API}/{fid}?supportsAllDrives=true', method='DELETE')
        except SystemExit:
            pass
    print(f'\ntrashed {len(trash)} test file(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
