"""A small tokenizer covering the Turtle-family syntax shared by SPARQL and stOTTR.

Tokens keep their source offsets so that parts of a query we do not interpret
(SPARQL expressions, FILTERs, ...) can be carried over verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class SyntaxErr(ValueError):
    pass


@dataclass
class Token:
    kind: str  # IRIREF PNAME VAR BNODE STRING LANGTAG NUMBER NAME PUNCT TYPEOPEN DIRECTIVE
    value: str
    start: int
    end: int
    prefix: str = ""  # PNAME only
    local: str = ""  # PNAME only
    numtype: str = ""  # NUMBER only: integer | decimal | double


_ESC = {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", '"': '"', "'": "'", "\\": "\\"}


def unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n in _ESC:
                out.append(_ESC[n])
                i += 2
                continue
            if n == "u":
                out.append(chr(int(s[i + 2 : i + 6], 16)))
                i += 6
                continue
            if n == "U":
                out.append(chr(int(s[i + 2 : i + 10], 16)))
                i += 10
                continue
        out.append(c)
        i += 1
    return "".join(out)


_PN_PREFIX = r"[A-Za-zÀ-￿](?:[\w.\-]*[\w\-])?"
_PN_LOCAL = r"(?:[\w%](?:[\w.\-%:]*[\w\-%:])?)"

_COMMON = [
    ("WS", r"\s+"),
    ("COMMENT", r"#[^\r\n]*"),
    ("IRIREF", r"<[^<>\"{}|^`\\\x00-\x20]*>"),
    ("STRING", r'"""(?:[^"\\]|\\.|"(?!""))*"""' r"|'''(?:[^'\\]|\\.|'(?!''))*'''" r'|"(?:[^"\\\r\n]|\\.)*"' r"|'(?:[^'\\\r\n]|\\.)*'"),
    ("VAR", r"[?$][\w][\w]*"),
    ("BNODE", r"_:[\w](?:[\w.\-]*[\w\-])?"),
    ("DTYPE", r"\^\^"),
]

_NUMBER_UNSIGNED = r"(?:\d+\.\d*[eE][+-]?\d+|\.\d+[eE][+-]?\d+|\d+[eE][+-]?\d+|\d*\.\d+|\d+)"

_MODES = {
    "sparql": _COMMON
    + [
        ("NUMBER", _NUMBER_UNSIGNED),
        ("PNAME", rf"(?:{_PN_PREFIX})?:{_PN_LOCAL}?"),
        ("NAME", r"[A-Za-z_][\w\-]*"),
        ("PUNCT", r"<<|>>|\{\||\|\}|&&|\|\||!=|<=|>=|[{}()\[\].,;|~!?=<>*/+\-@^&]"),
    ],
    "stottr": [("BLOCKCOMMENT", r"/\*\*\*.*?\*\*\*/")]
    + _COMMON
    + [
        ("TYPEOPEN", r"(?:NE)?List<|LUB<"),
        ("DIRECTIVE", r"@prefix\b|@base\b"),
        ("NUMBER", r"[+-]?" + _NUMBER_UNSIGNED),
        ("PUNCT", r"::|@@|\+\+"),
        ("PNAME", rf"(?:{_PN_PREFIX})?:(?!:){_PN_LOCAL}?"),
        ("NAME", r"[A-Za-z_][\w\-]*"),
        ("PUNCT", r"[{}()\[\].,;|!?=<>@]"),
    ],
}

_COMPILED = {m: re.compile("|".join(f"(?P<{k}{i}>{p})" for i, (k, p) in enumerate(rules)), re.S) for m, rules in _MODES.items()}
_LANG = re.compile(r"@[a-zA-Z]+(?:-[a-zA-Z0-9]+)*")


def tokenize(text: str, mode: str = "sparql") -> list[Token]:
    rx = _COMPILED[mode]
    toks: list[Token] = []
    pos = 0
    while pos < len(text):
        if text[pos] == "@" and toks and toks[-1].kind == "STRING" and toks[-1].end == pos:
            m = _LANG.match(text, pos)
            if m:
                toks.append(Token("LANGTAG", m.group(0)[1:], pos, m.end()))
                pos = m.end()
                continue
        m = rx.match(text, pos)
        if not m or m.end() == pos:
            line = text.count("\n", 0, pos) + 1
            raise SyntaxErr(f"unexpected character {text[pos]!r} at line {line}")
        kind = re.sub(r"\d+$", "", m.lastgroup)
        val = m.group(0)
        start, pos = pos, m.end()
        if kind in ("WS", "COMMENT", "BLOCKCOMMENT"):
            continue
        tok = Token(kind, val, start, pos)
        if kind == "IRIREF":
            tok.value = unescape(val[1:-1])
        elif kind == "STRING":
            q = 3 if val[:3] in ('"""', "'''") else 1
            tok.value = unescape(val[q:-q])
        elif kind == "VAR":
            tok.value = val[1:]
        elif kind == "BNODE":
            tok.value = val[2:]
        elif kind == "PNAME":
            tok.prefix, _, tok.local = val.partition(":")
        elif kind == "NUMBER":
            tok.numtype = "double" if "e" in val.lower() else "decimal" if "." in val else "integer"
        toks.append(tok)
    return toks


class Stream:
    def __init__(self, toks: list[Token], text: str):
        self.toks, self.text, self.i = toks, text, 0

    def peek(self, k: int = 0) -> Token | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def next(self) -> Token:
        t = self.peek()
        if t is None:
            raise SyntaxErr("unexpected end of input")
        self.i += 1
        return t

    def at(self, kind: str, value: str | None = None, ci: bool = False, k: int = 0) -> bool:
        t = self.peek(k)
        if t is None or t.kind != kind:
            return False
        if value is None:
            return True
        return t.value.lower() == value.lower() if ci else t.value == value

    def expect(self, kind: str, value: str | None = None, ci: bool = False) -> Token:
        if not self.at(kind, value, ci):
            t = self.peek()
            where = f"line {self.text.count(chr(10), 0, t.start) + 1}" if t else "end of input"
            got = repr(t.value) if t else "EOF"
            raise SyntaxErr(f"expected {value or kind} but found {got} at {where}")
        return self.next()

    def eof(self) -> bool:
        return self.i >= len(self.toks)
