"""Acceptance criterion 15: static files hold no colour literal outside
tokens.css and no way to run file text or change state."""

from __future__ import annotations

import re

from conftest import PKG_ROOT

STATIC = PKG_ROOT / "static"
NAMED = (
    "aliceblue antiquewhite aqua aquamarine azure beige bisque black "
    "blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse "
    "chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkgray "
    "darkgreen darkgrey darkorange darkred darkviolet deeppink deepskyblue "
    "dimgray dodgerblue firebrick forestgreen fuchsia gainsboro gold goldenrod "
    "gray green greenyellow grey hotpink indianred indigo ivory khaki lavender "
    "lawngreen lemonchiffon lightblue lightgray lightgreen lightgrey lightpink "
    "lime limegreen linen magenta maroon midnightblue mintcream navy oldlace "
    "olive orange orangered orchid pink plum purple rebeccapurple red "
    "royalblue salmon seagreen sienna silver skyblue slategray snow "
    "springgreen steelblue tan teal thistle tomato turquoise violet wheat "
    "white whitesmoke yellow yellowgreen"
).split()
COLOUR_PROPS = r"(?:color|background(?:-color)?|border(?:-[a-z]+)*|outline(?:-color)?|fill|stroke|box-shadow|text-decoration(?:-color)?|caret-color|accent-color)"


def _files() -> list:
    return sorted(p for p in STATIC.iterdir() if p.is_file())


def test_static_files_present() -> None:
    names = {p.name for p in _files()}
    assert names == {"index.html", "app.js", "render.js", "app.css", "tokens.css"}


def test_no_colour_literal_outside_tokens() -> None:
    hexre = re.compile(r"#[0-9a-fA-F]{3,8}\b")
    fnre = re.compile(r"\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(")
    named = re.compile(
        COLOUR_PROPS + r"\s*:[^;{}]*\b(" + "|".join(NAMED) + r")\b", re.IGNORECASE
    )
    for p in _files():
        if p.name == "tokens.css":
            continue
        text = p.read_text()
        assert not hexre.search(text), (p.name, hexre.search(text))
        assert not fnre.search(text), p.name
        assert not named.search(text), (p.name, named.search(text))
        if p.suffix == ".css":
            for m in re.finditer(COLOUR_PROPS + r"\s*:([^;}]*)", text):
                value = m.group(1)
                words = re.findall(r"[a-z]+", re.sub(r"var\([^)]*\)", "", value))
                assert not set(words) & set(NAMED), (p.name, value)


def test_tokens_define_the_variables_light_and_dark() -> None:
    text = (STATIC / "tokens.css").read_text()
    names = (
        "surface text muted border accent pass changes inconclusive escalated warn"
    ).split()
    root, _, dark = text.partition("@media (prefers-color-scheme: dark)")
    assert ":root" in root and ":root" in dark
    for n in names:
        assert re.search(rf"--{n}\s*:", root), n
        assert re.search(rf"--{n}\s*:", dark), n


def test_no_unsafe_dom_or_handlers() -> None:
    for p in _files():
        text = p.read_text()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert bad not in text, (p.name, bad)
        assert not re.search(r"\beval\s*\(", text), p.name
        assert not re.search(r"\bnew\s+Function\b", text), p.name
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", text, re.IGNORECASE), p.name
        assert not re.search(r"\.on[a-z]+\s*=", text), p.name
        assert not re.search(r"setAttribute\(\s*[\"']on", text), p.name


def test_no_state_changing_control() -> None:
    for p in _files():
        text = p.read_text()
        assert not re.search(r"<form\b", text, re.IGNORECASE), p.name
        assert not re.search(r"\bmethod\s*[:=]", text, re.IGNORECASE), p.name
        for m in re.finditer(r"fetch\(", text):
            call = text[m.start() : text.find(")", m.start()) + 1]
            assert "POST" not in call and "PUT" not in call, call


def test_index_loads_only_local_files() -> None:
    html = (STATIC / "index.html").read_text()
    for src in re.findall(r'(?:src|href)="([^"]*)"', html):
        assert src.startswith("/static/"), src
    assert "<style" not in html and "style=" not in html
    assert re.search(r"<script>", html) is None
