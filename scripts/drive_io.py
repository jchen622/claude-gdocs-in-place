#!/usr/bin/env python3
"""Pull and push a Drive file by id, without ever changing the id.

    python3 drive_io.py pull <file_id> <out_path>
    python3 drive_io.py push <file_id> <in_path> [--expect <modifiedTime> | --force]
    python3 drive_io.py stat <file_id>
    python3 drive_io.py pin  <file_id>
    python3 drive_io.py lost <file_id>

The id is the whole point. It carries the comment threads, their replies, the @-mentions and
the notifications they send, the resolution state, the share list, and every link anyone has
pasted into Slack or email. `push` is a media PATCH onto the existing id, so all of that
survives. There is deliberately no "create" verb here.

Four failure modes this wraps, all of which have cost real work:

  * **A push can fail silently.** `gcloud auth print-access-token` returns a short-lived token.
    When it expires mid-session the API returns an empty body, curl exits 0, and the next step
    happily reports success against a file that was never written. Every call here checks the
    HTTP status and raises.
  * **A push built on a stale pull silently reverts the author's work.** `push` refuses when the
    file moved on Drive after the pull this build came from, unless you pass --force. Without
    this guard a push once restored a deleted sentence and resurrected two resolved comments.
  * **Resolving a comment destroys it.** On a stored .docx, Google records a resolve as a
    *deletion*: the comment vanishes from the file, and Drive's comment layer keeps the row but
    strips the author and the content. So `pull` archives every comment it sees, from both
    sources, into an append-only ledger, and reports anything that has gone since last time.
  * **Google-native files are not binary.** Docs, Sheets and Slides must be exported via
    `/export?mimeType=...`; `alt=media` fails on them. A stored .docx/.pptx uploaded to Drive is
    binary and uses `alt=media`. `stat` prints the mimeType so you can tell which you have.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
DOCX = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
PPTX = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
NATIVE = {'application/vnd.google-apps.document',
          'application/vnd.google-apps.presentation',
          'application/vnd.google-apps.spreadsheet'}
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

LEDGER_DIR = pathlib.Path(
    os.environ.get('GDOCS_LEDGER_DIR', pathlib.Path.home() / '.gdocs-in-place' / 'ledger'))


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


# --------------------------------------------------------------------------- comment archiving

def _norm(s: str) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()


def _key(author: str, text: str) -> str:
    """Identity of a comment. Never its id: Google renumbers ids on every save."""
    return hashlib.sha1(f'{author}\x00{text}'.encode('utf-8')).hexdigest()[:16]


def docx_comments(path: str) -> list[dict]:
    """Comments inside the .docx itself, with their anchor text and reply/top-level status."""
    z = zipfile.ZipFile(path)
    if 'word/comments.xml' not in z.namelist():
        return []
    w14 = '{http://schemas.microsoft.com/office/word/2010/wordml}'
    w15 = '{http://schemas.microsoft.com/office/word/2012/wordml}'

    croot = ET.fromstring(z.read('word/comments.xml'))
    by_id, para_of = {}, {}
    for c in croot.iter(W + 'comment'):
        cid = c.get(W + 'id')
        by_id[cid] = {'author': c.get(W + 'author') or '?',
                      'date': c.get(W + 'date'),
                      'text': _norm(''.join(t.text or '' for t in c.iter(W + 't')))}
        for p in c.iter(W + 'p'):
            if p.get(w14 + 'paraId'):
                para_of[p.get(w14 + 'paraId')] = cid

    reply_of = {}
    if 'word/commentsExtended.xml' in z.namelist():
        ex = ET.fromstring(z.read('word/commentsExtended.xml'))
        for e in ex.iter(w15 + 'commentEx'):
            cid = para_of.get(e.get(w15 + 'paraId'))
            if cid:
                reply_of[cid] = bool(e.get(w15 + 'paraIdParent'))

    # anchor text per comment id
    droot = ET.fromstring(z.read('word/document.xml'))
    cur, spans = {}, {}
    for el in droot.iter():
        if el.tag == W + 'commentRangeStart':
            cur[el.get(W + 'id')] = []
        elif el.tag == W + 'commentRangeEnd':
            spans[el.get(W + 'id')] = _norm(''.join(cur.pop(el.get(W + 'id'), [])))
        elif el.tag in (W + 't', W + 'delText'):
            for buf in cur.values():
                buf.append(el.text or '')

    return [{'source': 'ooxml', 'author': v['author'], 'text': v['text'],
             'date': v['date'], 'anchor': spans.get(k, ''), 'isReply': reply_of.get(k, False)}
            for k, v in by_id.items()]


def drive_comments(fid: str) -> list[dict]:
    """Drive's own comment layer. A second, independent source: it holds replies that the OOXML
    does not. Deleted comments come back tombstoned, author and content stripped, so this cannot
    recover anything already resolved. Archive it for what is still live."""
    fields = ('comments(id,createdTime,author/displayName,content,resolved,deleted,'
              'quotedFileContent/value,replies(id,createdTime,author/displayName,content,'
              'deleted)),nextPageToken')
    out, page = [], None
    while True:
        q = {'includeDeleted': 'true', 'pageSize': '100', 'fields': fields}
        if page:
            q['pageToken'] = page
        _, body = _request(f'{API}/{fid}/comments?' + urllib.parse.urlencode(q))
        d = json.loads(body)
        for c in d.get('comments', []):
            if c.get('deleted') or not _norm(c.get('content', '')):
                continue          # tombstone: no author, no content, nothing to archive
            out.append({'source': 'drive',
                        'author': (c.get('author') or {}).get('displayName') or '?',
                        'text': _norm(c.get('content')),
                        'date': c.get('createdTime'),
                        'anchor': _norm((c.get('quotedFileContent') or {}).get('value', '')),
                        'isReply': False,
                        'replies': [{'author': (r.get('author') or {}).get('displayName') or '?',
                                     'text': _norm(r.get('content')),
                                     'date': r.get('createdTime')}
                                    for r in c.get('replies', [])
                                    if _norm(r.get('content'))]})
        page = d.get('nextPageToken')
        if not page:
            break
    return out


def _ledger_path(fid: str) -> pathlib.Path:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    return LEDGER_DIR / f'{fid}.json'


def load_ledger(fid: str) -> dict:
    p = _ledger_path(fid)
    if p.exists():
        return json.loads(p.read_text())
    return {'fileId': fid, 'pulls': [], 'comments': {}}


def update_ledger(fid: str, meta: dict, seen: list[dict]) -> dict:
    """Fold this pull's comments into the append-only ledger, and report what has gone.

    **Per source, not pooled.** Resolving a comment removes it from the .docx while Drive's
    comment layer often still returns it, so a union of the two hides exactly the event this
    exists to catch. One document had 15 comments deleted from the file and still live in the
    API; comparing the union reported nothing missing. Compare each source against itself.
    """
    led = load_ledger(fid)
    now = meta.get('modifiedTime')
    last = led['pulls'][-1] if led['pulls'] else {}
    prev = {'ooxml': set(last.get('docx_keys', [])),
            'drive': set(last.get('drive_keys', []))}

    cur = {'ooxml': set(), 'drive': set()}
    for c in seen:
        k = _key(c['author'], c['text'])
        cur.setdefault(c.get('source', 'ooxml'), set()).add(k)
        rec = led['comments'].get(k)
        if rec is None:
            led['comments'][k] = dict(c, firstSeen=now, lastSeen=now)
        else:
            rec['lastSeen'] = now
            if c.get('anchor') and not rec.get('anchor'):
                rec['anchor'] = c['anchor']
            if c.get('replies'):
                rec['replies'] = c['replies']

    def expand(keys):
        return [led['comments'][k] for k in keys if k in led['comments']]

    out = {
        'from_document': expand(prev['ooxml'] - cur['ooxml']),
        'from_drive_layer': expand(prev['drive'] - cur['drive']),
    }
    # Everything no longer in either source: the genuinely unrecoverable set.
    both = (prev['ooxml'] | prev['drive']) - (cur['ooxml'] | cur['drive'])
    out['from_both'] = expand(both)

    led['pulls'].append({'modifiedTime': now, 'name': meta.get('name'),
                         'count': len(cur['ooxml'] | cur['drive']),
                         'docx_keys': sorted(cur['ooxml']),
                         'drive_keys': sorted(cur['drive']),
                         'keys': sorted(cur['ooxml'] | cur['drive'])})
    led['name'] = meta.get('name')
    _ledger_path(fid).write_text(json.dumps(led, indent=1))
    return out


# --------------------------------------------------------------------------- verbs

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

    seen = []
    if out.endswith('.docx'):
        try:
            seen += docx_comments(out)
        except Exception as e:                       # never let archiving break a pull
            meta['comment_archive_error'] = str(e)[:200]
    try:
        seen += drive_comments(fid)
    except SystemExit as e:
        meta['drive_comment_error'] = str(e)[:200]

    if seen:
        gone = update_ledger(fid, meta, seen)
        meta['comments_archived'] = len(seen)
        meta['ledger'] = str(_ledger_path(fid))
        label = {'from_document': 'resolved_since_last_pull_gone_from_document',
                 'from_drive_layer': 'gone_from_drive_comment_layer',
                 'from_both': 'gone_from_both_sources'}
        for k, items in gone.items():
            if items:
                meta[label[k]] = [{'author': g['author'], 'text': g['text'][:160]}
                                  for g in items]
    return meta


def push(fid: str, path: str, expect: str | None = None, force: bool = False) -> dict:
    before = stat(fid)
    if not force:
        want = expect
        if want is None:
            led = load_ledger(fid)
            want = led['pulls'][-1]['modifiedTime'] if led.get('pulls') else None
        if want and want != before.get('modifiedTime'):
            raise SystemExit(
                'REFUSING TO PUSH: Drive moved since the pull this build came from.\n'
                f'  build is based on : {want}\n'
                f'  Drive now has     : {before.get("modifiedTime")} '
                f'(last edited by {(before.get("lastModifyingUser") or {}).get("displayName","?")})\n'
                'Pushing would revert their edits. Discard this build, pull again, redo the edit.\n'
                'Override with --force only if you are certain nothing was lost.')
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


def pin(fid: str) -> dict:
    """Set keepForever on every revision of the file.

    Revision history is the only way back to a comment somebody resolved, so pin early.

    Pinning also *restores access*. An unpinned revision older than about 30 days returns
    HTTP 403 on download, which looks exactly like the content having been purged. It has not
    been: pin it and the same revision downloads fine. Never read a 403 here as proof of loss.
    On the document this was built for, 32 revisions returned 403 and pinning brought all of
    them back, taking the recoverable comment history from 128 to 152."""
    _, body = _request(f'{API}/{fid}/revisions'
                       '?fields=revisions(id,modifiedTime,keepForever)&pageSize=1000'
                       '&supportsAllDrives=true')
    revs = json.loads(body).get('revisions', [])
    pinned, already, failed = [], [], []
    for r in revs:
        if r.get('keepForever'):
            already.append(r['modifiedTime'])
            continue
        try:
            _request(f'{API}/{fid}/revisions/{r["id"]}?supportsAllDrives=true',
                     method='PATCH', data=json.dumps({'keepForever': True}).encode(),
                     content_type='application/json')
            pinned.append(r['modifiedTime'])
        except SystemExit:
            failed.append(r['modifiedTime'])
    # Report what is actually *readable* now, since that is the question being asked.
    readable = 0
    for r in revs:
        try:
            _request(f'{API}/{fid}/revisions/{r["id"]}?alt=media&supportsAllDrives=true')
            readable += 1
        except SystemExit:
            pass
    return {'revisions': len(revs), 'newly_pinned': len(pinned),
            'already_pinned': len(already), 'pin_failed': len(failed),
            'content_readable': readable,
            'oldest_pinned': sorted(pinned + already)[0] if (pinned or already) else None}


def lost(fid: str) -> dict:
    """Everything the ledger has ever seen that is not in the most recent pull."""
    led = load_ledger(fid)
    if not led.get('pulls'):
        return {'error': 'no ledger yet for this file; run pull first'}
    last = led['pulls'][-1]
    cur = set(last['keys'])
    in_docx = set(last.get('docx_keys', []))
    gone = [v for k, v in led['comments'].items() if k not in cur]
    # Visible in neither the file nor the Docs UI, but still returned by the Drive API.
    hidden = [led['comments'][k] for k in cur - in_docx if k in led['comments']] \
        if last.get('docx_keys') else []
    return {'ever_seen': len(led['comments']), 'current': len(cur), 'lost': len(gone),
            'gone_from_document_but_live_in_drive_layer': len(hidden),
            'hidden': sorted(({'author': g['author'], 'text': g['text'][:120]} for g in hidden),
                             key=lambda g: (g['author'], g['text'])),
            'comments': sorted(({'author': g['author'], 'text': g['text'],
                                 'anchor': g.get('anchor', ''), 'lastSeen': g.get('lastSeen')}
                                for g in gone), key=lambda g: (g['author'], g['text']))}


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[2])
        return 2
    verb, fid = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    if verb == 'stat':
        print(json.dumps(stat(fid), indent=2))
    elif verb == 'pull':
        print(json.dumps(pull(fid, rest[0]), indent=2))
    elif verb == 'push':
        expect = rest[rest.index('--expect') + 1] if '--expect' in rest else None
        print(json.dumps(push(fid, rest[0], expect, '--force' in rest), indent=2))
    elif verb == 'pin':
        print(json.dumps(pin(fid), indent=2))
    elif verb == 'lost':
        print(json.dumps(lost(fid), indent=2))
    else:
        print(f'unknown verb {verb!r}; use pull, push, stat, pin or lost')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
