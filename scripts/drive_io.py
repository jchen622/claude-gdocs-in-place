#!/usr/bin/env python3
"""Pull and push a Drive file by id, without ever changing the id.

    python3 drive_io.py pull <file_id> <out_path>
    python3 drive_io.py push <file_id> <in_path>
    python3 drive_io.py stat <file_id>

The id is the whole point. It carries the comment threads, their replies, the @-mentions and
the notifications they send, the resolution state, the share list, and every link anyone has
pasted into Slack or email. `push` is a media PATCH onto the existing id, so all of that
survives. There is deliberately no "create" verb here.

Two failure modes this wraps, both of which have cost real work:

  * **A push can fail silently.** `gcloud auth print-access-token` returns a short-lived token.
    When it expires mid-session the API returns an empty body, curl exits 0, and the next step
    happily reports success against a file that was never written. Every call here checks the
    HTTP status and raises.
  * **Google-native files are not binary.** Docs, Sheets and Slides must be exported via
    `/export?mimeType=...`; `alt=media` fails on them. A stored .docx/.pptx uploaded to Drive is
    binary and uses `alt=media`. `stat` prints the mimeType so you can tell which you have.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
PPTX = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
NATIVE = {'application/vnd.google-apps.document',
          'application/vnd.google-apps.presentation',
          'application/vnd.google-apps.spreadsheet'}


def token() -> str:
    """Fresh access token. Re-run per invocation; these expire in about an hour."""
    out = subprocess.run(['gcloud', 'auth', 'print-access-token'],
                         capture_output=True, text=True)
    tok = out.stdout.strip()
    if out.returncode != 0 or not tok or ' ' in tok:
        raise SystemExit(f'could not get an access token: {out.stderr.strip()[:200]}')
    return tok


def _request(url: str, *, method='GET', data=None, content_type=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', f'Bearer {token()}')
    if content_type:
        req.add_header('Content-Type', content_type)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode('utf-8', 'replace')
        raise SystemExit(f'HTTP {e.code} from Drive: {body}')


def stat(fid: str) -> dict:
    fields = 'id,name,mimeType,modifiedTime,size,lastModifyingUser(displayName)'
    _, body = _request(f'{API}/{fid}?fields={fields}&supportsAllDrives=true')
    return json.loads(body)


def pull(fid: str, out: str) -> dict:
    meta = stat(fid)
    if meta['mimeType'] in NATIVE:
        raise SystemExit(
            f'{meta["mimeType"]} is a Google-native file. Use /export?mimeType=..., not alt=media. '
            'Native files have no .docx to edit; use the Docs/Slides API instead.')
    _, blob = _request(f'{API}/{fid}?alt=media&supportsAllDrives=true')
    with open(out, 'wb') as fh:
        fh.write(blob)
    meta['bytes_written'] = len(blob)
    return meta


def push(fid: str, path: str) -> dict:
    before = stat(fid)
    ctype = PPTX if path.endswith('.pptx') else DOCX
    with open(path, 'rb') as fh:
        blob = fh.read()
    url = (f'{UPLOAD}/{fid}?uploadType=media&supportsAllDrives=true'
           '&fields=id,name,modifiedTime,size')
    status, body = _request(url, method='PATCH', data=blob, content_type=ctype)
    after = json.loads(body)
    if after.get('id') != fid:
        raise SystemExit(f'id changed on push: {fid} -> {after.get("id")}. Aborting.')
    after['previous_modifiedTime'] = before.get('modifiedTime')
    return after


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[2])
        return 2
    verb, fid = sys.argv[1], sys.argv[2]
    if verb == 'stat':
        print(json.dumps(stat(fid), indent=2))
    elif verb == 'pull':
        print(json.dumps(pull(fid, sys.argv[3]), indent=2))
    elif verb == 'push':
        print(json.dumps(push(fid, sys.argv[3]), indent=2))
    else:
        print(f'unknown verb {verb!r}; use pull, push or stat')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
