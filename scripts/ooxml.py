#!/usr/bin/env python3
"""Shared OOXML helpers: read a .docx's three views, its comments, and its anchor spans.

The three views matter more than they sound. A document under review is really three documents
at once, and a safe edit has to know what it is doing to each:

  accept  what the text becomes if every tracked change is accepted
  reject  what it becomes if every tracked change is rejected
  raw     every character present, insertions and deletions alike

The single most useful invariant when editing a reviewed document is that **your edit must not
change the reject view at all**. If it does, you have altered settled text rather than proposing
a change, and whoever rejects your edit will not get back what they had.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
XS = '{http://www.w3.org/XML/1998/namespace}space'


def document(path: str) -> ET.Element:
    return ET.fromstring(zipfile.ZipFile(path).read('word/document.xml'))


def paragraphs(root: ET.Element, mode: str = 'accept') -> list[str]:
    """Non-empty paragraph texts under one of: accept, reject, raw."""
    out = []
    for para in root.iter(W + 'p'):
        buf: list[str] = []

        def walk(node, ins=False, dele=False):
            for ch in node:
                if ch.tag == W + 'ins':
                    walk(ch, True, dele)
                elif ch.tag == W + 'del':
                    walk(ch, ins, True)
                elif ch.tag in (W + 't', W + 'delText'):
                    t = ch.text or ''
                    if mode == 'raw':
                        buf.append(t)
                    elif mode == 'accept' and not dele:
                        buf.append(t)
                    elif mode == 'reject' and not ins:
                        buf.append(t)
                else:
                    walk(ch, ins, dele)

        walk(para)
        s = re.sub(r'\s+', ' ', ''.join(buf)).strip()
        if s:
            out.append(s)
    return out


def comments(path: str) -> list[tuple[str, str]]:
    """(author, text) per comment. Keyed on text, never on id: Docs renumbers ids on save."""
    z = zipfile.ZipFile(path)
    if 'word/comments.xml' not in z.namelist():
        return []
    root = ET.fromstring(z.read('word/comments.xml'))
    out = []
    for c in root.iter(W + 'comment'):
        text = re.sub(r'\s+', ' ', ''.join(t.text or '' for t in c.iter(W + 't'))).strip()
        out.append((c.get(W + 'author') or '?', text))
    return sorted(out)


def anchor_spans(root: ET.Element) -> dict[str, str]:
    """The text each comment is anchored to, by comment id.

    Worth checking on every edit. A comment whose anchor text is disturbed silently re-anchors,
    usually widening to the whole paragraph, and the reviewer loses the phrase they marked.
    """
    cur: dict[str, list[str]] = {}
    out: dict[str, str] = {}
    for el in root.iter():
        if el.tag == W + 'commentRangeStart':
            cur[el.get(W + 'id')] = []
        elif el.tag == W + 'commentRangeEnd':
            cid = el.get(W + 'id')
            out[cid] = re.sub(r'\s+', ' ', ''.join(cur.pop(cid, []))).strip()
        elif el.tag in (W + 't', W + 'delText'):
            for k in cur:
                cur[k].append(el.text or '')
    return out


def anchors_by_paragraph(root: ET.Element) -> dict[int, list[str]]:
    """Anchor text per paragraph, keyed by `id(paragraph_element)`.

    `anchor_spans()` answers "what is comment N anchored to". This answers "what is anchored in
    *this* paragraph", which is what an edit actually needs to know. Checking a match against
    every anchor in the document over-refuses badly: a comment anchored to a short, repeated
    string in one paragraph blocks an edit to an identical string anywhere else in the document.

    A comment range may open in one paragraph and close in a later one, so open ranges are
    carried forward and every paragraph they cross reports its own slice.
    """
    out: dict[int, list[str]] = {}
    carry: set[str] = set()
    for para in root.iter(W + 'p'):
        active: dict[str, list[str]] = {cid: [] for cid in carry}
        done: list[str] = []
        for el in para.iter():
            if el.tag == W + 'commentRangeStart':
                active[el.get(W + 'id')] = []
            elif el.tag == W + 'commentRangeEnd':
                chunk = active.pop(el.get(W + 'id'), None)
                if chunk is not None:
                    done.append(''.join(chunk))
            elif el.tag in (W + 't', W + 'delText'):
                for buf in active.values():
                    buf.append(el.text or '')
        done.extend(''.join(buf) for buf in active.values())
        carry = set(active)
        out[id(para)] = [s for s in (re.sub(r'\s+', ' ', t).strip() for t in done) if s]
    return out


def comments_extended(path: str) -> dict[str, bool]:
    """paraId -> resolved flag, from word/commentsExtended.xml.

    The format *can* represent a resolved comment (`w15:done="1"`). Google Docs never writes it:
    resolving deletes the comment outright, so every surviving entry reads `done="0"`. Read this
    when you need to prove that, not to find resolved comments, because there will not be any.
    """
    z = zipfile.ZipFile(path)
    name = 'word/commentsExtended.xml'
    if name not in z.namelist():
        return {}
    root = ET.fromstring(z.read(name))
    w15 = '{http://schemas.microsoft.com/office/word/2012/wordml}'
    return {e.get(w15 + 'paraId'): e.get(w15 + 'done') == '1'
            for e in root.iter(w15 + 'commentEx')}


def tracked(root: ET.Element) -> dict[str, int]:
    return {'ins': len(list(root.iter(W + 'ins'))), 'del': len(list(root.iter(W + 'del')))}


def rewrite(src: str, dst: str, root: ET.Element) -> None:
    """Write `root` back into a copy of `src`, leaving every other zip entry byte-identical."""
    blob = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    zin = zipfile.ZipFile(src)
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, blob if item.filename == 'word/document.xml'
                          else zin.read(item.filename))
