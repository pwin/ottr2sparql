"""Bi-directional conversion between TARQL/oxi-gen SPARQL CONSTRUCT mappings and OTTR templates."""

from .compose import ComposeResult, compose
from .decompose import Decomposition, decompose
from .ottr import Library, parse_stottr
from .shapes import generate_shapes
from .sparql import TarqlQuery, Unsupported, parse_query, serialize_query

__all__ = [
    "ComposeResult",
    "Decomposition",
    "Library",
    "TarqlQuery",
    "Unsupported",
    "compose",
    "decompose",
    "generate_shapes",
    "parse_query",
    "parse_stottr",
    "serialize_query",
]
