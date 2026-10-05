"""Typed literals the way oxi-gen writes them.

oxi-gen evaluates queries with spareval, which turns every typed value an
expression produces (a cast, ``STRDT``, a constant in a BIND) into its own
canonical form:

=================  ================================  ===========================
datatype           written as                        examples
=================  ================================  ===========================
xsd:boolean        ``true`` / ``false``              ``1`` -> ``true``
xsd:integer        no sign, no leading zeros         ``+007`` -> ``7``
xsd:decimal        no trailing zeros, no ``.0``      ``5000.00`` -> ``5000``
xsd:double/float   shortest form, no exponent        ``1.5E3`` -> ``1500``
xsd:dateTime       ``Z`` for UTC, fraction trimmed   ``...+00:00`` -> ``...Z``
=================  ================================  ===========================

Other datatypes, and values outside the lexical space, are left as written.
The SPARQL 1.1 casts (``xsd:integer(...)`` etc.) accept only the XSD lexical
form, without surrounding whitespace: ``xsd:dateTime("2019-03-01")`` is an error.
"""

from __future__ import annotations

import calendar
import re
import struct
from datetime import datetime, timedelta
from decimal import Decimal

from .terms import XSD

_INTEGER = re.compile(r"[+-]?\d+")
_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_DOUBLE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_SPECIAL = {"INF": "INF", "+INF": "INF", "-INF": "-INF", "NaN": "NaN"}
_DATETIME = re.compile(
    r"(?P<year>-?\d{4,})-(?P<month>\d\d)-(?P<day>\d\d)T(?P<hour>\d\d):(?P<minute>\d\d):(?P<second>\d\d)"
    r"(?P<fraction>\.\d+)?(?P<tz>Z|[+-]\d\d:\d\d)?"
)


def _plain_number(text: str) -> str:
    """A number in positional notation with no trailing zeros, as Rust prints a float."""
    out = format(Decimal(text), "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out


def _integer(lex: str) -> str | None:
    return str(int(lex)) if _INTEGER.fullmatch(lex) else None


def _decimal(lex: str) -> str | None:
    if not _DECIMAL.fullmatch(lex):
        return None
    d = Decimal(lex)
    return "0" if d == 0 else format(d.normalize(), "f")


def _double(lex: str) -> str | None:
    if lex in _SPECIAL:
        return _SPECIAL[lex]
    return _plain_number(repr(float(lex))) if _DOUBLE.fullmatch(lex) else None


def _float(lex: str) -> str | None:
    if lex in _SPECIAL:
        return _SPECIAL[lex]
    if not _DOUBLE.fullmatch(lex):
        return None
    f = struct.unpack("f", struct.pack("f", float(lex)))[0]
    for digits in range(1, 10):  # the shortest form that reads back as the same 32-bit float
        text = "%.*g" % (digits, f)
        if struct.unpack("f", struct.pack("f", float(text)))[0] == f:
            return _plain_number(text)
    return _plain_number(repr(f))


def _boolean(lex: str) -> str | None:
    return {"true": "true", "1": "true", "false": "false", "0": "false"}.get(lex)


def _datetime(lex: str) -> str | None:
    m = _DATETIME.fullmatch(lex)
    if not m:
        return None
    year, month, day = int(m["year"]), int(m["month"]), int(m["day"])
    hour, minute, second = int(m["hour"]), int(m["minute"]), int(m["second"])
    fraction = (m["fraction"] or "").rstrip("0").rstrip(".")
    month_days = [31, 29 if calendar.isleap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not (1 <= month <= 12 and 1 <= day <= month_days[month - 1] and minute < 60 and second < 60):
        return None
    if hour == 24:
        if minute or second or fraction:
            return None
        if not 1 <= year < 9999:
            return lex
        nxt = datetime(year, month, day) + timedelta(days=1)
        year, month, day, hour = nxt.year, nxt.month, nxt.day, 0
    elif hour > 23:
        return None
    tz = m["tz"] or ""
    if tz and tz != "Z":
        hours, minutes = int(tz[1:3]), int(tz[4:6])
        if minutes > 59 or hours * 60 + minutes > 14 * 60:
            return None
        if hours == minutes == 0:
            tz = "Z"
    sign = "-" if year < 0 else ""
    return f"{sign}{abs(year):04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}{fraction}{tz}"


CANONICAL = {
    XSD + "boolean": _boolean,
    XSD + "integer": _integer,
    XSD + "decimal": _decimal,
    XSD + "double": _double,
    XSD + "float": _float,
    XSD + "dateTime": _datetime,
}


def canonical_lexical(datatype: str, lexical: str) -> str | None:
    """oxi-gen's form of a value of ``datatype``, or None if ``lexical`` is not one.
    Datatypes oxi-gen does not rewrite give ``lexical`` back unchanged."""
    f = CANONICAL.get(datatype)
    return lexical if f is None else f(lexical)
