"""Minimal streaming VCD (IEEE 1364 value change dump) reader.

Standard library only: reads the header (timescale, scopes, $var
definitions), then keeps the value changes of the requested signals only,
so large dumps stay cheap. Covers scalar (0 1 x z), vector (b...) and real
(r...) changes; $comment / $dumpvars / $dumpoff blocks are handled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VcdVar:
    name: str  # full hierarchical name, e.g. "ALU_tb.result[31:0]"
    leaf: str  # reference without scope or range, e.g. "result"
    code: str  # VCD identifier code
    size: int
    kind: str  # wire, reg, integer, ...


@dataclass
class VcdHeader:
    timescale: str = ""
    vars: list[VcdVar] = field(default_factory=list)


def _tokens(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            yield from line.split()


def _until_end(tokens) -> list[str]:
    out = []
    for tok in tokens:
        if tok == "$end":
            break
        out.append(tok)
    return out


def read_vcd(path: Path, want: set[str] | None = None) -> tuple[VcdHeader, dict[str, list[tuple[int, str]]]]:
    """Returns (header, changes) where changes maps each wanted identifier
    code to its [(time, value), ...] in file order. want=None reads the
    header only."""
    header = VcdHeader()
    changes: dict[str, list[tuple[int, str]]] = {code: [] for code in (want or ())}
    tokens = _tokens(path)
    scopes: list[str] = []

    for tok in tokens:  # header
        if tok == "$timescale":
            header.timescale = " ".join(_until_end(tokens))
        elif tok == "$scope":
            body = _until_end(tokens)
            scopes.append(body[1] if len(body) > 1 else "?")
        elif tok == "$upscope":
            _until_end(tokens)
            if scopes:
                scopes.pop()
        elif tok == "$var":
            body = _until_end(tokens)
            if len(body) >= 4:
                kind, size, code, ref = body[0], body[1], body[2], body[3]
                rng = "".join(body[4:])
                header.vars.append(VcdVar(".".join([*scopes, ref]) + rng, ref, code, int(size) if size.isdigit() else 1, kind))
        elif tok == "$enddefinitions":
            _until_end(tokens)
            break
        elif tok.startswith("$"):
            _until_end(tokens)

    if not want:
        return header, changes

    time = 0
    for tok in tokens:  # value changes
        head = tok[0]
        if head == "#":
            time = int(tok[1:]) if tok[1:].isdigit() else time
        elif head in "bB":
            code = next(tokens, "")
            if code in changes:
                changes[code].append((time, tok[1:]))
        elif head in "rR":  # real value: keep the "r" so it isn't read as binary
            code = next(tokens, "")
            if code in changes:
                changes[code].append((time, "r" + tok[1:]))
        elif head in "01xXzZ" and len(tok) > 1:
            if tok[1:] in changes:
                changes[tok[1:]].append((time, head.lower()))
        elif tok == "$comment":
            _until_end(tokens)
        # $dumpvars / $dumpall / $dumpon / $dumpoff and their $end: the
        # changes inside them are ordinary value changes, so just continue.
    return header, changes


def format_value(raw: str, size: int) -> dict:
    """Binary vector -> {"value", "hex", "dec"} (hex/dec only when fully
    known); real ("r..." from read_vcd) -> {"value", "real"}."""
    if raw.startswith("r"):
        try:
            return {"value": raw[1:], "real": float(raw[1:])}
        except ValueError:
            return {"value": raw[1:]}
    if size <= 1 or not set(raw) <= set("01xz"):
        return {"value": raw}
    padded = raw.rjust(size, raw[0] if raw[0] in "xz" else "0")
    out = {"value": padded}
    if set(padded) <= {"0", "1"}:
        n = int(padded, 2)
        out["hex"] = f"{n:0{(size + 3) // 4}x}"
        out["dec"] = n
    return out
