from __future__ import annotations

import csv
import datetime as dt
import json
import math
import zipfile
from pathlib import Path
from typing import Any


DEFAULT_QA_CRITERIA: dict[str, float] = {
    "median_qc_rsd_percent_max": 30.0,
    "qc_features_rsd_le_30_fraction_min": 0.70,
    "median_qc_detection_rate_min": 0.80,
    "sample_blank_ratio_ge_3_fraction_min": 0.70,
    "qc_pca_relative_dispersion_max": 0.50,
    "median_blank_carryover_ratio_max": 0.10,
    "run_order_intensity_abs_correlation_max": 0.30,
}


def generate_publication_report(
    workflow: dict[str, Any],
    qa_report: dict[str, Any] | None,
    output_root: str | Path,
    *,
    app_version: str,
    console_version: str,
    qa_criteria: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    criteria = _criteria(qa_criteria)
    qa_assessment = assess_qa(qa_report, criteria)
    provenance_warnings = _library_warnings(workflow)
    methods = _methods_text(
        workflow, qa_report, qa_assessment, app_version, console_version
    )
    results = _results_text(qa_report, qa_assessment)
    rows = supplementary_rows(
        workflow,
        qa_report,
        qa_assessment,
        app_version=app_version,
        console_version=console_version,
    )

    methods_path = root / "MS_DIAL_Materials_and_Methods.txt"
    results_path = root / "MS_DIAL_QA_Results.txt"
    table_path = root / "Supplementary_Table_MS_DIAL.tsv"
    audit_path = root / "MS_DIAL_publication_report.json"
    bundle_path = root / "MS_DIAL_publication_reporting_bundle.zip"

    methods_path.write_text(methods + "\n", encoding="utf-8")
    results_path.write_text(results + "\n", encoding="utf-8")
    with table_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Section", "Record", "Parameter", "Value", "Unit or note", "Source"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    audit = {
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "software": {
            "msdial_console_version": console_version or "not recorded",
            "msdial_interactive_version": app_version,
        },
        "qa_assessment": qa_assessment,
        "library_provenance_warnings": provenance_warnings,
        "workflow": workflow,
        "qa_report": qa_report,
    }
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (methods_path, results_path, table_path, audit_path):
            archive.write(path, path.name)
    return {
        "methods_text": methods,
        "qa_results_text": results,
        "qa_assessment": qa_assessment,
        "warnings": provenance_warnings,
        "methods_file": str(methods_path),
        "qa_results_file": str(results_path),
        "supplementary_table": str(table_path),
        "audit_file": str(audit_path),
        "bundle": str(bundle_path),
    }


def assess_qa(
    qa_report: dict[str, Any] | None,
    criteria: dict[str, float] | None = None,
) -> dict[str, Any]:
    resolved = _criteria(criteria)
    summary = (qa_report or {}).get("summary", {})
    checks = [
        _check(summary, "median_qc_rsd_percent", "Median QC feature RSD", "<=", resolved["median_qc_rsd_percent_max"], "%"),
        _check(summary, "qc_features_rsd_le_30_percent", "Fraction of QC features with RSD <=30%", ">=", resolved["qc_features_rsd_le_30_fraction_min"], "fraction"),
        _check(summary, "median_qc_detection_rate", "Median QC detection rate", ">=", resolved["median_qc_detection_rate_min"], "fraction"),
        _check(summary, "sample_blank_ratio_ge_3", "Fraction of features with Sample/Blank >=3", ">=", resolved["sample_blank_ratio_ge_3_fraction_min"], "fraction"),
        _check(summary, "qc_pca_relative_dispersion", "QC PCA relative dispersion", "<=", resolved["qc_pca_relative_dispersion_max"], "ratio"),
        _check(summary, "median_blank_carryover_ratio", "Median blank carryover ratio", "<=", resolved["median_blank_carryover_ratio_max"], "fraction"),
        _check(summary, "run_order_intensity_correlation", "Absolute run-order/intensity correlation", "abs<=", resolved["run_order_intensity_abs_correlation_max"], "correlation"),
    ]
    evaluated = [item for item in checks if item["status"] != "not_assessed"]
    passed = [item for item in evaluated if item["status"] == "pass"]
    return {
        "status": "not_assessed" if not evaluated else ("pass" if len(passed) == len(evaluated) else "review"),
        "passed": len(passed),
        "evaluated": len(evaluated),
        "checks": checks,
        "criteria": resolved,
    }


def supplementary_rows(
    workflow: dict[str, Any],
    qa_report: dict[str, Any] | None,
    qa_assessment: dict[str, Any],
    *,
    app_version: str,
    console_version: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(section: str, record: str, parameter: str, value: Any, note: str = "", source: str = "MS-DIAL Interactive") -> None:
        rows.append(
            {
                "Section": section,
                "Record": record,
                "Parameter": parameter,
                "Value": _value(value),
                "Unit or note": note,
                "Source": source,
            }
        )

    add("Software", "MS-DIAL Console", "Version", console_version or "not recorded")
    add("Software", "MS-DIAL Interactive", "Version", app_version)
    for index, file in enumerate(workflow.get("files", []), start=1):
        record = str(file.get("file_name") or f"Analysis file {index}")
        for key in ("file_path", "file_name", "file_type", "class_id", "raw_format", "vendor", "acquisition_type", "batch_order", "analytical_order", "factor"):
            if key in file:
                note = "Local absolute path; review before publication" if key == "file_path" else ""
                add("Data", record, key, file.get(key), note)

    annotation_keys = {
        "msp_annotators", "text_annotators", "selected_lipids", "selected_adducts",
        "library_provenance", "lbm_path", "lbm_rt_tolerance", "lbm_ms1_tolerance",
        "lbm_ms2_tolerance", "lbm_weighted_dot_product", "lbm_simple_dot_product",
        "lbm_reverse_dot_product", "lbm_matched_peaks_percentage",
        "lbm_minimum_spectrum_match", "lbm_use_rt_scoring", "lbm_use_rt_filtering",
    }
    excluded = {"files", "console_path", "template_path", "output_root"} | annotation_keys
    for key in sorted(workflow):
        if key not in excluded:
            add("Guided setup", "Workflow", key, workflow[key])
    for key in ("console_path", "template_path", "output_root"):
        if key in workflow:
            add("Run audit", "Local environment", key, workflow[key], "Local path; omit from publication table if required")

    for index, item in enumerate(workflow.get("msp_annotators", []), start=1):
        _add_mapping(add, "Annotation", str(item.get("annotator_id") or f"MSP annotator {index}"), item)
    for index, item in enumerate(workflow.get("text_annotators", []), start=1):
        _add_mapping(add, "Annotation", str(item.get("annotator_id") or f"Text annotator {index}"), item)
    lbm = {key: workflow.get(key) for key in sorted(annotation_keys) if key.startswith("lbm_") and key in workflow}
    if any(value not in (None, "") for value in lbm.values()):
        _add_mapping(add, "Annotation", "LBM annotator", lbm)
    for index, adduct in enumerate(workflow.get("selected_adducts", []), start=1):
        add("Annotation", f"Adduct {index}", "selected_adduct", adduct)
    for index, lipid in enumerate(workflow.get("selected_lipids", []), start=1):
        _add_mapping(add, "Annotation", f"Lipid query {index}", lipid)
    for index, library in enumerate(workflow.get("library_provenance", []), start=1):
        _add_mapping(add, "Library provenance", str(library.get("label") or f"Library {index}"), library)

    if qa_report:
        for key, value in sorted((qa_report.get("summary") or {}).items()):
            add("Quality assurance", "Observed metric", key, value)
        for item in qa_assessment.get("checks", []):
            add("Quality assurance", item["label"], "Observed", item.get("value"), item.get("unit", ""))
            add("Quality assurance", item["label"], "Criterion", f"{item['operator']} {item['threshold']}", item.get("unit", ""))
            add("Quality assurance", item["label"], "Assessment", item["status"])
        for index, standard in enumerate(qa_report.get("internal_standards", []), start=1):
            _add_mapping(add, "Quality assurance", str(standard.get("name") or f"Internal standard {index}"), {key: value for key, value in standard.items() if key != "values"})
    return rows


def _methods_text(
    workflow: dict[str, Any],
    qa_report: dict[str, Any] | None,
    assessment: dict[str, Any],
    app_version: str,
    console_version: str,
) -> str:
    files = workflow.get("files", [])
    project_type = str(workflow.get("project_type", "lcms")).lower()
    project = {
        "lcms": "LC-MS",
        "gcms": "GC-MS",
        "dims": "DI-MS",
        "lcimms": "LC-IM-MS",
        "imms": "IM-MS",
        "imaging": "imaging MS",
    }.get(project_type, project_type.upper())
    ion = str(workflow.get("ion_mode", "not recorded")).lower()
    omics = str(workflow.get("target_omics", "not recorded")).lower()
    console = console_version if console_version and console_version != "not recorded" else "[VERSION NOT RECORDED]"
    interactive = app_version if app_version and app_version != "not recorded" else "[VERSION NOT RECORDED]"
    paragraphs = [
        "MS-DIAL data processing",
        (
            f"Raw mass spectrometry data ({len(files)} analysis files) were processed using "
            f"MS-DIAL Console version {console} through MS-DIAL Interactive version {interactive}. "
            f"The workflow was configured for {project} {ion}-ion {omics} analysis. Complete sample "
            "metadata, processing parameters, annotation settings, and library provenance are provided "
            "in Supplementary Table S1."
        ),
        _processing_sentence(workflow, project_type),
        _annotation_sentence(workflow),
    ]
    if workflow.get("execute_rt_correction"):
        paragraphs.append(
            "Retention-time correction was applied using the anchor library and reviewed peak selections documented in Supplementary Table S1."
        )
    paragraphs.extend(["Quality assurance", _qa_methods_sentence(qa_report, assessment)])
    return "\n\n".join(paragraphs)


def _processing_sentence(workflow: dict[str, Any], project_type: str) -> str:
    peak = (
        f"Features were detected after {workflow.get('smoothing_method', 'not recorded')} smoothing "
        f"using a minimum peak height of {_value(workflow.get('minimum_peak_height'))} and a minimum "
        f"peak width of {_value(workflow.get('minimum_peak_width'))} data points."
    )
    if project_type == "gcms":
        retention = workflow.get("gcms_retention_type", "RT")
        alignment = workflow.get("gcms_alignment_index_type", "RT")
        ri = workflow.get("gcms_ri_compound_type", "not recorded")
        return (
            f"{peak} Electron-ionization data were treated as "
            f"{_value(workflow.get('gcms_accuracy_type'))} mass data. The retention coordinate used for "
            f"annotation was {retention}, and alignment used {alignment}; the configured RI compound type "
            f"was {ri}."
        )
    return (
        f"{peak} The mass-slice width was {_value(workflow.get('mass_slice_width'))} Da. MS1 and MS2 "
        f"centroid tolerances were {_value(workflow.get('ms1_tolerance'))} and "
        f"{_value(workflow.get('ms2_tolerance'))} Da, respectively. Features were aligned with "
        f"retention-time and MS1 tolerances of {_value(workflow.get('alignment_rt_tolerance'))} min and "
        f"{_value(workflow.get('alignment_ms1_tolerance'))} Da."
    )


def _annotation_sentence(workflow: dict[str, Any]) -> str:
    msp_count = len(workflow.get("msp_annotators", []))
    text_count = len(workflow.get("text_annotators", []))
    lbm = bool(str(workflow.get("lbm_path", "")).strip())
    types = []
    if msp_count:
        types.append(f"{msp_count} MSP annotator{'s' if msp_count != 1 else ''}")
    if text_count:
        types.append(f"{text_count} text-library annotator{'s' if text_count != 1 else ''}")
    if lbm:
        types.append("one LBM annotator")
    description = ", ".join(types) if types else "the annotation resources listed in Supplementary Table S1"
    cited = []
    for item in workflow.get("library_provenance", []):
        identifier = item.get("doi") or item.get("record_url")
        if identifier:
            cited.append(f"{item.get('label') or item.get('filename') or 'library'} ({identifier})")
    citation = f" Downloaded libraries were {', '.join(cited)}." if cited else ""
    return f"Molecular annotation used {description} with the database-specific settings reported in Supplementary Table S1.{citation}"


def _qa_methods_sentence(qa_report: dict[str, Any] | None, assessment: dict[str, Any]) -> str:
    if not qa_report:
        return "No LC-MS quality-assurance matrix was supplied when this report was generated; QA claims should be added after assessment."
    summary = qa_report.get("summary", {})
    counts = summary.get("category_counts", {})
    lead = (
        f"Analytical quality was assessed from {summary.get('sample_count', 0)} files "
        f"({counts.get('Sample', 0)} {_plural(counts.get('Sample', 0), 'study sample')}, "
        f"{counts.get('QC', 0)} {_plural(counts.get('QC', 0), 'pooled QC sample')}, and "
        f"{counts.get('Blank', 0)} {_plural(counts.get('Blank', 0), 'blank')}) using feature-intensity distributions, QC precision and detection "
        "rate, blank separation and carryover, PCA topology, analytical-order drift, MS/MS acquisition, "
        "raw signal-to-noise ratios, and internal-standard mass and retention-time errors where available."
    )
    if not assessment["evaluated"]:
        return lead + " Prespecified QA criteria could not be evaluated from the available sample types."
    return lead + f" {assessment['passed']} of {assessment['evaluated']} prespecified, evaluable QA criteria were met; individual criteria and outcomes are reported in Supplementary Table S1."


def _results_text(qa_report: dict[str, Any] | None, assessment: dict[str, Any]) -> str:
    if not qa_report:
        return "Quality-assurance results were not generated because no LC-MS QA matrix was supplied."
    summary = qa_report.get("summary", {})
    pieces = [f"Quality assessment included {summary.get('sample_count', 0)} injections and {summary.get('alignment_spot_count', 0)} aligned features."]
    if summary.get("median_qc_rsd_percent") is not None:
        pieces.append(f"The median feature RSD among QC injections was {summary['median_qc_rsd_percent']:.1f}%.")
    if summary.get("median_qc_detection_rate") is not None:
        pieces.append(f"The median QC detection rate was {summary['median_qc_detection_rate'] * 100:.1f}%.")
    if summary.get("qc_pca_relative_dispersion") is not None:
        pieces.append(f"QC relative dispersion in the first two PCA dimensions was {summary['qc_pca_relative_dispersion']:.3f} compared with all displayed samples.")
    pieces.append(f"Overall, {assessment['passed']} of {assessment['evaluated']} evaluable prespecified QA criteria were met.")
    failed = [item["label"] for item in assessment["checks"] if item["status"] == "fail"]
    if failed:
        pieces.append("Criteria requiring review were: " + "; ".join(failed) + ".")
    return " ".join(pieces)


def _library_warnings(workflow: dict[str, Any]) -> list[str]:
    provenance = workflow.get("library_provenance", [])
    by_path = {str(item.get("local_path", "")).casefold(): item for item in provenance}
    warnings = []
    paths = [str(item.get("msp_file_path", "")) for item in workflow.get("msp_annotators", [])]
    paths += [str(item.get("text_db_file_path", "")) for item in workflow.get("text_annotators", [])]
    if workflow.get("lbm_path"):
        paths.append(str(workflow["lbm_path"]))
    for path in filter(None, paths):
        item = by_path.get(path.casefold())
        if not item or not (item.get("doi") or item.get("record_url")):
            warnings.append(
                f"No persistent identifier was recorded for {Path(path).name}. Add a database version, DOI, repository URL, or checksum before publication."
            )
    return warnings


def _criteria(values: dict[str, Any] | None) -> dict[str, float]:
    result = dict(DEFAULT_QA_CRITERIA)
    for key in result:
        if values and values.get(key) not in (None, ""):
            result[key] = float(values[key])
    return result


def _check(summary: dict[str, Any], key: str, label: str, operator: str, threshold: float, unit: str) -> dict[str, Any]:
    value = summary.get(key)
    if value is None or not _finite(value):
        status = "not_assessed"
    elif operator == "<=":
        status = "pass" if float(value) <= threshold else "fail"
    elif operator == ">=":
        status = "pass" if float(value) >= threshold else "fail"
    else:
        status = "pass" if abs(float(value)) <= threshold else "fail"
    return {"metric": key, "label": label, "value": value, "operator": operator, "threshold": threshold, "unit": unit, "status": status}


def _add_mapping(add: Any, section: str, record: str, mapping: dict[str, Any]) -> None:
    for key, value in mapping.items():
        add(section, record, str(key), value)


def _value(value: Any) -> str:
    if value is None:
        return "not recorded"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _plural(count: Any, noun: str) -> str:
    return noun if int(count or 0) == 1 else noun + "s"
