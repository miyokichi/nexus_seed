"""Source document adapters: business files become Canonical YAML v0.1.

This package is the *left* edge of the loop::

    Excel / PowerPoint / Database  ->  Canonical YAML  ->  Knowledge

It is deliberately not part of the Knowledge Runtime.  A converter reads one
source format and emits the Canonical contract; it never touches the Ledger,
the Knowledge Gateway, or a Knowledge backend such as Semantica.  Adding a
second source format therefore changes nothing on the Knowledge side.

Not to be confused with :mod:`nexus_seed.observation_sources`, which is about
*observing* folders at runtime rather than converting a document.
"""

from .excel import (
    EntitySpec,
    ExcelMapping,
    ExcelSourceError,
    FieldSpec,
    PropertySpec,
    RelationSpec,
    convert_excel,
    convert_excel_to_yaml,
    load_excel_mapping,
    loads_excel_mapping,
)

__all__ = [
    "EntitySpec",
    "ExcelMapping",
    "ExcelSourceError",
    "FieldSpec",
    "PropertySpec",
    "RelationSpec",
    "convert_excel",
    "convert_excel_to_yaml",
    "load_excel_mapping",
    "loads_excel_mapping",
]
