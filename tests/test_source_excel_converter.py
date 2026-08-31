"""One .xlsx sheet becomes Canonical YAML v0.1, and stops being Excel there."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_seed.modules.knowledge.canonical import loads_canonical_yaml
from nexus_seed.sources import (
    ExcelSourceError,
    convert_excel,
    convert_excel_to_yaml,
    load_excel_mapping,
    loads_excel_mapping,
)


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "tests" / "fixtures" / "assumption.xlsx"
MAPPING = ROOT / "samples" / "excel_assumption_mapping.yaml"


def test_workbook_becomes_a_validated_canonical_document():
    document = convert_excel(WORKBOOK, MAPPING)

    assert document.schema_version == "0.1"
    assert document.document_id == "assumption-001"
    assert document.source_file == "assumption.xlsx"
    assert document.source_location == "GenX"
    assert [entity.id for entity in document.entities] == [
        "gen_x",
        "wl_width",
        "performance_report",
    ]


def test_entities_and_properties_are_separated():
    document = convert_excel(WORKBOOK, MAPPING)
    entities = {entity.id: entity for entity in document.entities}

    assert entities["gen_x"].type == "Generation"
    assert entities["gen_x"].name == "GenX"
    assert entities["gen_x"].properties == {}

    parameter = entities["wl_width"]
    assert parameter.type == "Parameter"
    assert parameter.aliases == ("WL width",)
    assert parameter.properties == {
        "typical": {"value": 50, "unit": "nm"},
        "variation": {"value": 10, "unit": "nm"},
    }

    deliverable = entities["performance_report"]
    assert deliverable.type == "Deliverable"
    assert deliverable.properties == {"required": True, "status": "missing"}


def test_explicit_relations_come_from_the_mapping_not_from_inference():
    document = convert_excel(WORKBOOK, MAPPING)

    assert [
        (relation.subject, relation.predicate, relation.object)
        for relation in document.explicit_relations
    ] == [
        ("gen_x", "uses_parameter", "wl_width"),
        ("gen_x", "requires", "performance_report"),
    ]


def test_provenance_carries_file_sheet_and_cell_range():
    document = convert_excel(WORKBOOK, MAPPING)
    sources = {entity.id: entity.source for entity in document.entities}

    assert sources["gen_x"].location == "GenX!A2:B2"
    assert sources["wl_width"].location == "GenX!C2:H2"
    assert sources["performance_report"].location == "GenX!I2:L2"
    assert document.explicit_relations[0].source.location == "GenX!A2:H2"
    assert all(
        source.document_id == "assumption-001" for source in sources.values()
    )


def test_yaml_output_reloads_through_the_canonical_boundary(tmp_path):
    text = convert_excel_to_yaml(WORKBOOK, MAPPING)
    path = tmp_path / "assumption.yaml"
    path.write_text(text, encoding="utf-8")

    reloaded = loads_canonical_yaml(path.read_text(encoding="utf-8"))

    assert reloaded.to_dict() == convert_excel(WORKBOOK, MAPPING).to_dict()


def test_workbook_shape_stays_inside_provenance():
    """Sheets, columns and cells are locators, never entity content."""

    document = convert_excel(WORKBOOK, MAPPING)

    for entity in document.entities:
        assert "GenX!" not in str(entity.properties)
        assert set(entity.properties) <= {"typical", "variation", "required", "status"}
    payload = document.to_dict()
    assert set(payload["document"]["source"]) == {"file", "location"}
    assert all(
        set(entity["source"]) == {"document_id", "location"}
        for entity in payload["entities"]
    )


def test_text_view_states_the_same_facts_in_sentences():
    document = convert_excel(WORKBOOK, MAPPING)

    assert "WL Width (Parameter): typical = 50 nm, variation = 10 nm." in document.text
    assert "GenX uses parameter WL Width." in document.text
    assert "GenX requires Performance Evaluation Report." in document.text


def test_document_id_can_be_overridden_per_conversion():
    document = convert_excel(WORKBOOK, MAPPING, document_id="assumption-2026-03")

    assert document.document_id == "assumption-2026-03"
    assert all(
        entity.source.document_id == "assumption-2026-03"
        for entity in document.entities
    )


def test_repeated_subject_rows_produce_one_entity(tmp_path):
    workbook = _workbook(
        tmp_path,
        "Sheet1",
        ["Generation ID", "Generation", "Parameter ID", "Parameter", "Typical"],
        [
            ["gen_x", "GenX", "wl_width", "WL Width", 50],
            ["gen_x", "GenX", "pitch", "Pitch", 90],
        ],
    )
    document = convert_excel(workbook, _mapping(sheet="Sheet1"))

    assert [entity.id for entity in document.entities] == ["gen_x", "wl_width", "pitch"]
    assert document.entities[0].source.location == "Sheet1!A2:B2"
    assert [relation.object for relation in document.explicit_relations] == [
        "wl_width",
        "pitch",
    ]


def test_a_row_without_an_id_simply_contributes_no_entity(tmp_path):
    workbook = _workbook(
        tmp_path,
        "Sheet1",
        ["Generation ID", "Generation", "Parameter ID", "Parameter", "Typical"],
        [
            ["gen_x", "GenX", "wl_width", "WL Width", 50],
            ["gen_y", "GenY", None, None, None],
        ],
    )
    document = convert_excel(workbook, _mapping(sheet="Sheet1"))

    assert [entity.id for entity in document.entities] == ["gen_x", "wl_width", "gen_y"]
    assert len(document.explicit_relations) == 1


def test_a_column_the_mapping_names_but_the_sheet_lacks_is_an_error(tmp_path):
    workbook = _workbook(
        tmp_path, "Sheet1", ["Generation ID", "Generation"], [["gen_x", "GenX"]]
    )
    with pytest.raises(ExcelSourceError, match="Parameter ID"):
        convert_excel(workbook, _mapping(sheet="Sheet1"))


def test_a_missing_sheet_names_the_sheets_that_exist(tmp_path):
    workbook = _workbook(
        tmp_path, "Sheet1", ["Generation ID", "Generation"], [["gen_x", "GenX"]]
    )
    with pytest.raises(ExcelSourceError, match="Sheet1"):
        convert_excel(workbook, _mapping(sheet="Absent"))


def test_an_empty_sheet_is_refused_rather_than_producing_an_empty_document(tmp_path):
    workbook = _workbook(tmp_path, "Sheet1", ["Generation ID", "Generation"], [])
    with pytest.raises(ExcelSourceError, match="no data rows"):
        convert_excel(workbook, _mapping(sheet="Sheet1"))


def test_a_relation_between_unknown_entity_keys_is_refused():
    with pytest.raises(ExcelSourceError, match="unknown entity key"):
        loads_excel_mapping(
            """
            document: {id: "d"}
            sheet: "Sheet1"
            entities:
              - {key: a, id: {column: "A"}, type: "T"}
            relations:
              - {subject: a, predicate: "p", object: "missing"}
            """
        )


def test_a_field_with_both_a_column_and_a_literal_is_refused():
    with pytest.raises(ExcelSourceError, match="exactly one"):
        loads_excel_mapping(
            """
            document: {id: "d"}
            sheet: "Sheet1"
            entities:
              - {key: a, id: {column: "A", value: "a"}, type: "T"}
            """
        )


def test_the_shipped_mapping_sample_loads():
    mapping = load_excel_mapping(MAPPING)

    assert mapping.document_id == "assumption-001"
    assert mapping.sheet == "GenX"
    assert [entity.key for entity in mapping.entities] == [
        "generation",
        "parameter",
        "deliverable",
    ]


def test_an_entity_type_outside_the_ontology_is_still_valid_canonical(tmp_path):
    """The converter validates the Canonical contract; vocabulary is Knowledge's job."""

    workbook = _workbook(
        tmp_path, "Sheet1", ["Generation ID", "Generation"], [["gen_x", "GenX"]]
    )
    mapping = loads_excel_mapping(
        """
        document: {id: "d"}
        sheet: "Sheet1"
        entities:
          - key: generation
            id: {column: "Generation ID"}
            name: {column: "Generation"}
            type: "SomethingNotInTheOntology"
        """
    )
    document = convert_excel(workbook, mapping)

    assert document.entities[0].type == "SomethingNotInTheOntology"


def _mapping(*, sheet: str):
    return loads_excel_mapping(
        f"""
        document: {{id: "assumption-test"}}
        sheet: "{sheet}"
        entities:
          - key: generation
            id: {{column: "Generation ID"}}
            name: {{column: "Generation"}}
            type: "Generation"
          - key: parameter
            id: {{column: "Parameter ID"}}
            name: {{column: "Parameter"}}
            type: "Parameter"
            properties:
              typical: {{column: "Typical"}}
        relations:
          - {{subject: generation, predicate: "uses_parameter", object: parameter}}
        """
    )


def _workbook(tmp_path: Path, sheet: str, headers: list[str], rows: list[list]) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    path = tmp_path / "book.xlsx"
    workbook.save(path)
    return path


def test_cli_writes_canonical_yaml_that_the_boundary_accepts(tmp_path, capsys):
    from nexus_seed.source_cli import main

    destination = tmp_path / "out" / "assumption.yaml"
    exit_code = main(
        [
            "excel",
            str(WORKBOOK),
            "--mapping",
            str(MAPPING),
            "--out",
            str(destination),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == str(destination)
    document = loads_canonical_yaml(destination.read_text(encoding="utf-8"))
    assert document.document_id == "assumption-001"


def test_cli_prints_to_stdout_without_an_output_path(tmp_path, capsys):
    from nexus_seed.source_cli import main

    assert main(["excel", str(WORKBOOK), "--mapping", str(MAPPING)]) == 0

    assert loads_canonical_yaml(capsys.readouterr().out).source_file == "assumption.xlsx"


def test_cli_reports_a_conversion_error_instead_of_a_traceback(tmp_path, capsys):
    from nexus_seed.source_cli import main

    exit_code = main(
        ["excel", str(tmp_path / "absent.xlsx"), "--mapping", str(MAPPING)]
    )

    assert exit_code == 2
    assert "conversion error" in capsys.readouterr().err


def test_a_short_row_reads_as_empty_cells_not_as_a_missing_column(tmp_path):
    workbook = _workbook(
        tmp_path,
        "Sheet1",
        ["Generation ID", "Generation", "Parameter ID", "Parameter", "Typical"],
        [["gen_x", "GenX", "wl_width", "WL Width", 50], ["gen_y", "GenY"]],
    )
    document = convert_excel(workbook, _mapping(sheet="Sheet1"))

    assert [entity.id for entity in document.entities] == ["gen_x", "wl_width", "gen_y"]
