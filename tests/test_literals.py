"""oxi-gen's forms of typed literals (checked against oxi-gen v0.5.1, spareval 0.2)."""

import pytest

from ottr_tarql.literals import canonical_lexical
from ottr_tarql.terms import XSD


@pytest.mark.parametrize(
    "datatype, lexical, written",
    [
        ("integer", "007", "7"),
        ("integer", "+7", "7"),
        ("integer", "-0", "0"),
        ("integer", " 7 ", None),  # no whitespace collapse
        ("integer", "7.0", None),
        ("decimal", "5000.00", "5000"),
        ("decimal", "0.50", "0.5"),
        ("decimal", "+1.50", "1.5"),
        ("decimal", "-0.0", "0"),
        ("decimal", ".5", "0.5"),
        ("decimal", "5.", "5"),
        ("decimal", "1e3", None),
        ("double", "1e3", "1000"),
        ("double", "1.5E3", "1500"),
        ("double", "-1.25E-2", "-0.0125"),
        ("double", "-0.0", "-0"),
        ("double", "1e21", "1000000000000000000000"),
        ("double", "INF", "INF"),
        ("double", "NaN", "NaN"),
        ("float", "0.1", "0.1"),
        ("float", "1.0e0", "1"),
        ("boolean", "1", "true"),
        ("boolean", "0", "false"),
        ("boolean", "TRUE", None),
        ("dateTime", "2019-03-01T09:30:00+00:00", "2019-03-01T09:30:00Z"),
        ("dateTime", "2019-03-01T09:30:00-00:00", "2019-03-01T09:30:00Z"),
        ("dateTime", "2019-03-01T09:30:00.250Z", "2019-03-01T09:30:00.25Z"),
        ("dateTime", "2019-03-01T09:30:00.0Z", "2019-03-01T09:30:00Z"),
        ("dateTime", "2019-03-01T24:00:00", "2019-03-02T00:00:00"),
        ("dateTime", "2019-12-31T24:00:00Z", "2020-01-01T00:00:00Z"),
        ("dateTime", "2019-03-01T09:30:00+01:00", "2019-03-01T09:30:00+01:00"),
        ("dateTime", "2020-02-29T00:00:00", "2020-02-29T00:00:00"),
        ("dateTime", "2019-02-29T00:00:00", None),
        ("dateTime", "2019-03-01", None),
        ("dateTime", "2019-03-01T09:30:00+15:00", None),
        ("date", "2019-03-01+00:00", "2019-03-01+00:00"),  # oxi-gen rewrites date-times only
        ("gYear", "0998", "0998"),
    ],
)
def test_canonical_lexical(datatype, lexical, written):
    assert canonical_lexical(XSD + datatype, lexical) == written
