#!/usr/bin/env python3
"""Inject _static/*.html and _static/*.js into app.py so it's fully self-contained.

Run this whenever you edit anything under _static/. The shipped artefact is
app.py; the _static/ folder is source-only.
"""
from __future__ import annotations
from pathlib import Path

root = Path(__file__).resolve().parent
src = root / "app.py"
text = src.read_text("utf-8")

idx = (root / "_static/index.html").read_text("utf-8")
js = (root / "_static/app.js").read_text("utf-8")
sw = (root / "_static/sw.js").read_text("utf-8")
mini = (root / "_static/mini_wrapper.html").read_text("utf-8")

idx = idx.replace("__INJECT_APP_JS__", js)

for blob_name, blob in [("index", idx), ("sw", sw), ("mini", mini)]:
    if '"""' in blob:
        raise SystemExit(f"{blob_name} contains triple double-quotes; cannot embed")


def find_block(label: str) -> tuple[int, int]:
    needle = f'"""__INJECT_{label}__"""'
    p = text.find(needle)
    if p < 0:
        # Already replaced — find prior assignment and replace its body.
        start_marker = f"{label}_HTML = " if label != "SW" else "SERVICE_WORKER_JS = "
        if label == "INDEX":
            start_marker = "INDEX_HTML = "
        elif label == "MINI":
            start_marker = "MINI_APP_WRAPPER = "
        elif label == "SW":
            start_marker = "SERVICE_WORKER_JS = "
        i = text.index(start_marker)
        # Find the opening """
        q1 = text.index('"""', i)
        # Find the matching closing """ — assume no nested triple quotes in payload
        q2 = text.index('"""', q1 + 3)
        return q1, q2 + 3
    return p, p + len(needle)


def replace_block(t: str, label: str, payload: str) -> str:
    s, e = find_block_in(t, label)
    return t[:s] + '"""' + payload + '"""' + t[e:]


def find_block_in(t: str, label: str) -> tuple[int, int]:
    needle = f'"""__INJECT_{label}__"""'
    p = t.find(needle)
    if p >= 0:
        return p, p + len(needle)
    if label == "INDEX":
        prefix = "INDEX_HTML = "
    elif label == "SW":
        prefix = "SERVICE_WORKER_JS = "
    elif label == "MINI":
        prefix = "MINI_APP_WRAPPER = "
    else:
        raise ValueError(label)
    i = t.index(prefix)
    q1 = t.index('"""', i)
    q2 = t.index('"""', q1 + 3)
    return q1, q2 + 3


for label, payload in [("INDEX", idx), ("SW", sw), ("MINI", mini)]:
    text = replace_block(text, label, payload)

src.write_text(text, "utf-8")
print("built app.py;", len(text), "chars")
