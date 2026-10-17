#!/usr/bin/env python3
"""Measure what Google actually does to a comment when it is resolved.

Runs against throwaway Drive files, never a real document.

  python3 -I experiment.py setup     upload test.docx (stored) and a native Doc copy
  python3 -I experiment.py state     print both sources for both files
  python3 -I experiment.py resolve   resolve one comment on each, via the Drive API
  python3 -I experiment.py doneflag  push a .docx with w15:done="1" and see what Drive makes of it
  python3 -I experiment.py teardown  trash both test files

State is kept in ids.json beside this script.
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path.home() / '.claude/skills/gdocs-in-place/scripts'))
from drive_io import API, UPLOAD, DOCX, _request, token, stat, docx_comments  # noqa: E402
from ooxml import comments_extended  # noqa: E402

HERE = pathlib.Path(__file__).parent
IDS = HERE / 'ids.json'
CF = ('comments(id,author/displayName,content,resolved,deleted,quotedFileContent/value,'
      'replies(id,author/displayName,content,action,deleted))')


def ids() -> dict:
    return json.loads(IDS.read_text()) if IDS.exists() else {}


def save(d): IDS.write_text(json.dumps(d, indent=1))


def upload(path: pathlib.Path, name: str, native: bool) -> str:
    import uuid
    meta = {'name': name}
    if native:
        meta['mimeType'] = 'application/vnd.google-apps.document'
    b = '===' + uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'
            f'{json.dumps(meta)}\r\n--{b}\r\nContent-Type: {DOCX}\r\n\r\n').encode()
    body += path.read_bytes() + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request(
        f'{UPLOAD}?uploadType=multipart&fields=id,name,mimeType',
        data=body, method='POST',
        headers={'Authorization': 'Bearer ' + token(),
                 'Content-Type': f'multipart/related; boundary={b}'})
    return json.load(urllib.request.urlopen(req))['id']


def drive_comments_raw(fid: str) -> list[dict]:
    q = urllib.parse.urlencode({'includeDeleted': 'true', 'pageSize': '100', 'fields': CF})
    _, body = _request(f'{API}/{fid}/comments?{q}')
    return json.loads(body).get('comments', [])


def show(tag: str, fid: str, native: bool):
    print(f'\n--- {tag}  ({fid})')
    cs = drive_comments_raw(fid)
    print(f'    Drive comment layer: {len(cs)} thread(s)')
    for c in cs:
        a = (c.get('author') or {}).get('displayName')
        print(f'      resolved={str(c.get("resolved")):5} deleted={str(c.get("deleted")):5} '
              f'author={a!r} content={(c.get("content") or "")[:46]!r}'
              f' replies={len(c.get("replies", []))}')
    if not native:
        tmp = HERE / f'_pull_{fid[:8]}.docx'
        _, blob = _request(f'{API}/{fid}?alt=media&supportsAllDrives=true')
        tmp.write_bytes(blob)
        dc = docx_comments(str(tmp))
        print(f'    inside the .docx   : {len(dc)} comment(s), '
              f'done flags={list(comments_extended(str(tmp)).values())}')
        for c in dc:
            print(f'      {c["author"]!r}: {c["text"][:46]!r}')


def main() -> int:
    verb = sys.argv[1]
    d = ids()
    if verb == 'setup':
        d['docx'] = upload(HERE / 'test.docx', 'ZZ_comment_test_stored_docx', native=False)
        d['native'] = upload(HERE / 'test.docx', 'ZZ_comment_test_native_doc', native=True)
        save(d)
        print(json.dumps(d, indent=1))
        for k in ('docx', 'native'):
            print(f'  {k}: https://docs.google.com/document/d/{d[k]}/edit')
    elif verb == 'state':
        show('stored .docx', d['docx'], False)
        show('native Google Doc', d['native'], True)
    elif verb == 'resolve':
        for k in ('docx', 'native'):
            cs = [c for c in drive_comments_raw(d[k]) if not c.get('deleted')]
            if not cs:
                print(f'  {k}: no comments in the Drive layer, cannot resolve via API')
                continue
            target = next((c for c in cs if 'resolved' in (c.get('content') or '')), cs[0])
            payload = json.dumps({'content': 'Resolving via the Drive API for this test.',
                                  'action': 'resolve'}).encode()
            _, body = _request(
                f'{API}/{d[k]}/comments/{target["id"]}/replies?fields=id,action,content',
                method='POST', data=payload, content_type='application/json')
            print(f'  {k}: resolved {target["id"]} -> {json.loads(body)}')
    elif verb == 'doneflag':
        # Push a .docx whose comment 2 is marked resolved in the format's own way, then read
        # it back. Tests whether Drive preserves w15:done="1" and surfaces it anywhere.
        import subprocess
        src = HERE / 'test_done.docx'
        subprocess.run([sys.executable, '-I', str(HERE / 'make_docx.py'), str(src),
                        '--done', '2'], check=True)
        blob = src.read_bytes()
        _, body = _request(
            f'{UPLOAD}/{d["docx"]}?uploadType=media&fields=id,modifiedTime',
            method='PATCH', data=blob, content_type=DOCX)
        print('  pushed:', json.loads(body))
        back = HERE / '_doneflag_back.docx'
        _, b2 = _request(f'{API}/{d["docx"]}?alt=media&supportsAllDrives=true')
        back.write_bytes(b2)
        print('  done flags read back from Drive:', comments_extended(str(back)))
        print('  comments still in file         :', len(docx_comments(str(back))))
        print('  Drive comment layer            :',
              len(drive_comments_raw(d['docx'])), 'thread(s)')
    elif verb == 'teardown':
        for k in ('docx', 'native'):
            if d.get(k):
                _request(f'{API}/{d[k]}', method='DELETE')
                print(f'  trashed {k} {d[k]}')
        IDS.unlink(missing_ok=True)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
