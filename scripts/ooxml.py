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
