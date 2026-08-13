from __future__ import annotations

import datetime as dt
import json
import math
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape


DATA_COLUMNS = [
    "file_path",
    "file_name",
    "file_type",
    "class_id",
    "acquisition_type",
    "batch_order",
    "analytical_order",
    "factor",
]

GUIDED_SECTIONS = [
    (
        "Software versions",
        [
            ("msdial_console_version", "MS-DIAL Console version", ""),
            ("msdial_interactive_version", "MS-DIAL Interactive version", ""),
        ],
    ),
    (
        "Core workflow",
        [
            ("project_type", "Project type", ""),
            ("ion_mode", "Ion mode", ""),
            ("target_omics", "Target omics", ""),
            ("solvent", "Solvent type", "Used for lipidomics annotation"),
            ("ms1_data_type", "MS1 data type", ""),
            ("ms2_data_type", "MS2 data type", ""),
            ("number_of_threads", "Number of threads", ""),
        ],
    ),
    (
        "Peak picking",
        [
            ("smoothing_method", "Smoothing method", ""),
            ("minimum_peak_height", "Minimum peak height", "intensity"),
            ("mass_slice_width", "Mass slice width", "Da"),
            ("minimum_peak_width", "Minimum peak width", "data points"),
            ("retention_time_begin", "Retention time begin", "min"),
            ("retention_time_end", "Retention time end", "min"),
            ("ms1_tolerance", "MS1 tolerance", "Da"),
            ("ms2_tolerance", "MS2 tolerance", "Da"),
        ],
    ),
    (
        "Alignment",
        [
            ("together_with_alignment", "Run alignment", ""),
            ("alignment_rt_tolerance", "Retention time tolerance", "min"),
            ("alignment_ms1_tolerance", "MS1 tolerance", "Da"),
            ("alignment_light_mode", "Alignment-light mode", ""),
        ],
    ),
    (
        "Retention time correction",
        [
            ("execute_rt_correction", "Apply RT correction", ""),
            ("rt_correction_anchor_path", "Anchor library path", "Local path"),
            ("rt_correction_anchor_source_path", "Original anchor library path", "Local path"),
            ("rt_correction_selection_path", "Reviewed peak selections", "Local path"),
            ("rt_correction_diff_method", "RT difference method", ""),
            ("rt_correction_smooth_rt_diff", "Smooth RT differences", ""),
            ("rt_correction_intercept", "Intercept at RT begin", "min"),
            ("rt_correction_extrapolation_begin", "Extrapolation at RT begin", ""),
            ("rt_correction_extrapolation_end", "Extrapolation at RT end", ""),
            ("rt_correction_peak_selection_mode", "Automatic peak selection", ""),
            ("rt_correction_peak_selection_rt_weight", "RT-priority weight", "0-1"),
        ],
    ),
    (
        "GC-MS retention index",
        [
            ("gcms_accuracy_type", "Mass accuracy type", ""),
            ("gcms_retention_type", "Retention coordinate for annotation", ""),
            ("gcms_alignment_index_type", "Retention coordinate for alignment", ""),
            ("gcms_ri_compound_type", "RI compound type", ""),
            ("gcms_ri_alignment_tolerance", "RI alignment tolerance", "RI units"),
            ("gcms_ri_source", "RI dictionary source", ""),
            ("gcms_ri_standard_path", "RI standard path", "Local path"),
            ("gcms_ri_dictionary_path", "RI dictionary path", "Local path"),
        ],
    ),
    (
        "Execution and audit paths",
        [
            ("console_path", "MS-DIAL Console path", "Local path; review before publication"),
            ("template_path", "Parameter template path", "Local path; review before publication"),
            ("output_root", "Output root", "Local path; review before publication"),
            ("stage_inputs", "Stage/copy input files", ""),
        ],
    ),
]

ANNOTATION_KEYS = {
    "msp_annotators",
    "text_annotators",
    "selected_lipids",
    "selected_adducts",
    "library_provenance",
    "msp_path",
    "text_db_path",
    "msp_annotator_settings_file_path",
    "text_annotator_settings_file_path",
}
ANNOTATION_PREFIXES = ("msp_", "text_", "lbm_")


def write_supplementary_workbook(
    path: str | Path,
    workflow: dict[str, Any],
    qa_report: dict[str, Any] | None,
    qa_assessment: dict[str, Any],
    *,
    app_version: str,
    console_version: str,
) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook_state = dict(workflow)
    workbook_state["msdial_console_version"] = console_version or "not recorded"
    workbook_state["msdial_interactive_version"] = app_version or "not recorded"
    sheets = [
        _data_sheet(workbook_state),
        _guided_sheet(workbook_state),
        _annotation_sheet(workbook_state),
        _qa_sheet(qa_report, qa_assessment),
    ]
    _write_xlsx(target, sheets)
    return target


def _data_sheet(workflow: dict[str, Any]) -> dict[str, Any]:
    rows = [_row(DATA_COLUMNS, "header")]
    for item in workflow.get("files", []):
        row = _row([item.get(key, "") for key in DATA_COLUMNS])
        if len(str(item.get("file_path", ""))) > 65:
            row["height"] = 32
        rows.append(row)
    return {
        "name": "Data",
        "rows": rows,
        "widths": [52, 28, 14, 18, 18, 12, 16, 14],
        "freeze_rows": 1,
        "auto_filter": f"A1:H{max(1, len(rows))}",
    }


def _guided_sheet(workflow: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    represented: set[str] = set()
    is_gcms = str(workflow.get("project_type", "lcms")).lower() == "gcms"
    uses_rt_correction = bool(workflow.get("execute_rt_correction"))
    is_lipidomics = str(workflow.get("target_omics", "")).lower() == "lipidomics"
    for section, fields in GUIDED_SECTIONS:
        if section == "GC-MS retention index" and not is_gcms:
            continue
        if section == "Retention time correction" and not uses_rt_correction:
            fields = fields[:1]
        present = [(key, label, note) for key, label, note in fields if key in workflow]
        if not is_lipidomics:
            present = [item for item in present if item[0] != "solvent"]
        if not present:
            continue
        rows.append(_section(section, 3))
        rows.append(_row(["Field", "Value", "Unit or note"], "header"))
        for key, label, note in present:
            rows.append(_row([label, _guided_value(key, workflow.get(key)), note], "body_left"))
            represented.add(key)

    excluded = {"files"} | ANNOTATION_KEYS
    remaining = [
        key
        for key in sorted(workflow)
        if key not in represented
        and key not in excluded
        and not key.startswith(ANNOTATION_PREFIXES)
        and key != "gcms_ri_file_map"
        and not (not is_gcms and key.startswith("gcms_"))
        and not (not uses_rt_correction and key.startswith("rt_correction_"))
        and not (not is_lipidomics and key == "solvent")
    ]
    if remaining:
        rows.append(_section("Additional recorded settings", 3))
        rows.append(_row(["Field", "Value", "Unit or note"], "header"))
        for key in remaining:
            rows.append(_row([_label(key), _typed_value(workflow[key]), ""], "body_left"))

    ri_map = workflow.get("gcms_ri_file_map", []) if is_gcms else []
    if ri_map:
        rows.append(_section("GC-MS per-file RI mapping", 3))
        rows.append(_row(["Analysis file", "Analysis path", "RI standard path"], "header"))
        for item in ri_map:
            rows.append(
                _row(
                    [
                        item.get("file_name", ""),
                        item.get("file_path", ""),
                        item.get("ri_path", ""),
                    ],
                    "body_left",
                )
            )
    return {
        "name": "Guided setup",
        "rows": rows or [_row(["No guided setup was recorded."])],
        "widths": [34, 72, 38],
        "freeze_rows": 0,
    }


def _annotation_sheet(workflow: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(workflow.get("msp_annotators", []), start=1):
        name = item.get("annotator_id") or f"MSP annotator {index}"
        _mapping_section(rows, f"MSP annotator: {name}", item)
    for index, item in enumerate(workflow.get("text_annotators", []), start=1):
        name = item.get("annotator_id") or f"Text annotator {index}"
        _mapping_section(rows, f"Text library annotator: {name}", item)

    lbm = {
        key: workflow[key]
        for key in sorted(workflow)
        if key.startswith("lbm_") and workflow.get(key) not in (None, "")
    }
    if lbm:
        _mapping_section(rows, "LBM annotator", lbm)

    adducts = workflow.get("selected_adducts", [])
    if adducts:
        rows.append(_section("Selected adducts", 3))
        rows.append(_row(["No.", "Adduct", "Ion mode"], "header"))
        ion_mode = workflow.get("ion_mode", "")
        for index, adduct in enumerate(adducts, start=1):
            rows.append(_row([index, adduct, ion_mode], "body_left"))

    lipids = workflow.get("selected_lipids", [])
    if lipids:
        rows.append(_section("Selected lipid queries", 3))
        rows.append(_row(["Lipid class", "Adduct", "Ion mode"], "header"))
        for item in lipids:
            rows.append(
                _row(
                    [
                        item.get("lipid_class", ""),
                        item.get("adduct", ""),
                        item.get("ion_mode", ""),
                    ],
                    "body_left",
                )
            )

    provenance = workflow.get("library_provenance", [])
    if provenance:
        rows.append(_section("Library provenance", 8))
        rows.append(
            _row(
                [
                    "Library",
                    "Version",
                    "DOI",
                    "Repository URL",
                    "License",
                    "Checksum (MD5)",
                    "File name",
                    "Local path",
                ],
                "header",
            )
        )
        for item in provenance:
            rows.append(
                _row(
                    [
                        item.get("label", ""),
                        item.get("version", ""),
                        item.get("doi", ""),
                        item.get("record_url", ""),
                        item.get("license", ""),
                        item.get("md5", ""),
                        item.get("filename", ""),
                        item.get("local_path", ""),
                    ],
                    "body_left",
                )
            )

    return {
        "name": "Annotation",
        "rows": rows or [_row(["No annotation settings were recorded."])],
        "widths": [34, 28, 28, 42, 18, 36, 36, 70],
        "freeze_rows": 0,
    }


def _qa_sheet(
    qa_report: dict[str, Any] | None, qa_assessment: dict[str, Any]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if not qa_report:
        rows.append(_section("Quality assurance", 5))
        rows.append(_row(["No LC-MS QA matrix was supplied."]))
    else:
        rows.append(_section("Observed summary", 5))
        rows.append(_row(["Metric", "Value", "Unit or note", "Criterion", "Assessment"], "header"))
        checks = {item["metric"]: item for item in qa_assessment.get("checks", [])}
        for key, value in sorted((qa_report.get("summary") or {}).items()):
            if isinstance(value, (dict, list, tuple)):
                value = _display_value(value)
            check = checks.get(key)
            criterion = ""
            status = ""
            note = ""
            if check:
                criterion = f"{check['operator']} {check['threshold']}"
                status = check["status"]
                note = check.get("unit", "")
            label = check["label"] if check else _label(key)
            rows.append(_row([label, _typed_value(value), note, criterion, status], "body_left"))

        standards = qa_report.get("internal_standards", [])
        for index, item in enumerate(standards, start=1):
            name = item.get("name") or f"Internal standard {index}"
            mapping = {key: value for key, value in item.items() if key != "values"}
            _mapping_section(rows, f"Internal standard: {name}", mapping, columns=5)
    return {
        "name": "Quality assurance",
        "rows": rows,
        "widths": [40, 24, 22, 20, 18],
        "freeze_rows": 0,
    }


def _mapping_section(
    rows: list[dict[str, Any]],
    title: str,
    mapping: dict[str, Any],
    *,
    columns: int = 3,
) -> None:
    rows.append(_section(title, columns))
    header = ["Field", "Value", "Unit or note"] + [""] * max(0, columns - 3)
    rows.append(_row(header, "header"))
    for key, value in mapping.items():
        body = [_label(key), _typed_value(value), ""] + [""] * max(0, columns - 3)
        rows.append(_row(body, "body_left"))


def _row(values: Iterable[Any], style: str = "body") -> dict[str, Any]:
    return {"values": list(values), "style": style}


def _section(title: str, columns: int) -> dict[str, Any]:
    return {"values": [title] + [""] * (columns - 1), "style": "section", "merge": columns}


def _label(key: str) -> str:
    overrides = {
        "rt": "RT",
        "ms1": "MS1",
        "ms2": "MS2",
        "msp": "MSP",
        "lbm": "LBM",
        "ri": "RI",
        "gcms": "GC-MS",
        "id": "ID",
        "doi": "DOI",
        "md5": "MD5",
        "qc": "QC",
        "pca": "PCA",
        "sn": "S/N",
        "msms": "MS/MS",
    }
    words = []
    for part in key.split("_"):
        words.append(overrides.get(part.lower(), part))
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def _guided_value(key: str, value: Any) -> Any:
    if key == "project_type":
        return {
            "lcms": "LC-MS",
            "gcms": "GC-MS",
            "dims": "DI-MS",
            "lcimms": "LC-IM-MS",
            "imms": "IM-MS",
            "imaging": "Imaging MS",
        }.get(str(value).lower(), _typed_value(value))
    return _typed_value(value)


def _typed_value(value: Any) -> Any:
    if value is None:
        return "not recorded"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(float(value)) else str(value)
    if isinstance(value, (dict, list, tuple)):
        return _display_value(value)
    return str(value)


def _display_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _write_xlsx(path: Path, sheets: list[dict[str, Any]]) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(len(sheets)))
        archive.writestr("_rels/.rels", _package_relationships())
        archive.writestr("docProps/core.xml", _core_properties(timestamp))
        archive.writestr("docProps/app.xml", _app_properties([sheet["name"] for sheet in sheets]))
        archive.writestr("xl/workbook.xml", _workbook_xml(sheets))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_relationships(len(sheets)))
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, sheet in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(sheet))


def _sheet_xml(sheet: dict[str, Any]) -> str:
    rows = sheet.get("rows", [])
    max_columns = max((len(row.get("values", [])) for row in rows), default=1)
    last_cell = f"{_column_name(max_columns)}{max(1, len(rows))}"
    column_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(sheet.get("widths", []), start=1)
    )
    row_xml: list[str] = []
    merges: list[str] = []
    style_ids = {"body": 0, "header": 1, "section": 2, "body_left": 3}
    for row_index, row in enumerate(rows, start=1):
        values = row.get("values", [])
        style = style_ids.get(row.get("style", "body"), 0)
        cells = "".join(
            _cell_xml(row_index, column_index, value, style)
            for column_index, value in enumerate(values, start=1)
        )
        height_value = row.get("height")
        if height_value is None and row.get("style") in {"header", "section"}:
            height_value = 24
        height = f' ht="{height_value}" customHeight="1"' if height_value else ""
        row_xml.append(f'<row r="{row_index}"{height}>{cells}</row>')
        merge = int(row.get("merge", 0) or 0)
        if merge > 1:
            merges.append(f'A{row_index}:{_column_name(merge)}{row_index}')
    freeze_rows = int(sheet.get("freeze_rows", 0) or 0)
    pane = ""
    if freeze_rows:
        pane = (
            f'<pane ySplit="{freeze_rows}" topLeftCell="A{freeze_rows + 1}" '
            'activePane="bottomLeft" state="frozen"/>'
        )
    merge_xml = ""
    if merges:
        merge_xml = f'<mergeCells count="{len(merges)}">' + "".join(
            f'<mergeCell ref="{item}"/>' for item in merges
        ) + "</mergeCells>"
    auto_filter = sheet.get("auto_filter")
    filter_xml = f'<autoFilter ref="{escape(str(auto_filter))}"/>' if auto_filter else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_cell}"/><sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        f'{pane}</sheetView></sheetViews><sheetFormatPr defaultRowHeight="18"/>'
        f'<cols>{column_xml}</cols><sheetData>{"".join(row_xml)}</sheetData>'
        f'{merge_xml}{filter_xml}</worksheet>'
    )


def _cell_xml(row: int, column: int, value: Any, style: int) -> str:
    reference = f"{_column_name(column)}{row}"
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        return f'<c r="{reference}" s="{style}" t="n"><v>{value}</v></c>'
    text = _xml_text(value)
    preserve = ' xml:space="preserve"' if text != text.strip() else ""
    return f'<c r="{reference}" s="{style}" t="inlineStr"><is><t{preserve}>{escape(text)}</t></is></c>'


def _xml_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _content_types(sheet_count: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        f'{sheets}</Types>'
    )


def _package_relationships() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )


def _workbook_xml(sheets: list[dict[str, Any]]) -> str:
    entries = "".join(
        f'<sheet name="{escape(sheet["name"])}" sheetId="{index}" r:id="rId{index}"/>'
        for index, sheet in enumerate(sheets, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<bookViews><workbookView/></bookViews><sheets>{entries}</sheets></workbook>'
    )


def _workbook_relationships(sheet_count: int) -> str:
    sheets = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{sheets}<Relationship Id="rId{sheet_count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="3">'
        '<font><sz val="11"/><name val="Arial"/><family val="2"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/><family val="2"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="12"/><name val="Arial"/><family val="2"/></font>'
        '</fonts>'
        '<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF147D82"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF164E63"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="2"><border/><border><bottom style="thin"><color rgb="FFD7E3E5"/></bottom></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="4">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="left" vertical="top" wrapText="1"/></xf>'
        '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _core_properties(timestamp: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:creator>MS-DIAL Interactive</dc:creator><cp:lastModifiedBy>MS-DIAL Interactive</cp:lastModifiedBy>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified></cp:coreProperties>'
    )


def _app_properties(sheet_names: list[str]) -> str:
    titles = "".join(f'<vt:lpstr>{escape(name)}</vt:lpstr>' for name in sheet_names)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>MS-DIAL Interactive</Application><DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop>'
        '<HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant>'
        f'<vt:variant><vt:i4>{len(sheet_names)}</vt:i4></vt:variant></vt:vector></HeadingPairs>'
        f'<TitlesOfParts><vt:vector size="{len(sheet_names)}" baseType="lpstr">{titles}</vt:vector></TitlesOfParts>'
        '</Properties>'
    )
