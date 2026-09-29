from __future__ import annotations

import csv
import datetime as dt
import json
import math
import zipfile
from pathlib import Path
from typing import Any

from .automatic_rt_evidence import automatic_rt_correction_proof, unproven
from .quality_assurance import with_qc_minimum
from .supplementary_excel import write_supplementary_workbook


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
    # A QA report from before 0.5.3 is read as 0.5.3 would have written it, so that the text, the table
    # and the audit all apply the same QC minimum and carry the same reasons.
    if qa_report and isinstance(qa_report.get("summary"), dict):
        qa_report = {**qa_report, "summary": with_qc_minimum(qa_report["summary"])}
    report_workflow = dict(workflow)
    report_workflow["automatic_rt_correction_evidence"] = (
        _automatic_rt_correction_evidence(root, workflow)
    )
    criteria = _criteria(qa_criteria)
    qa_assessment = assess_qa(qa_report, criteria)
    provenance_warnings = _library_warnings(report_workflow)
    automatic_rt_evidence = report_workflow["automatic_rt_correction_evidence"]
    if automatic_rt_evidence.get("requested") and not automatic_rt_evidence.get("performed"):
        provenance_warnings.append(
            "Automatic alignment RT correction was requested, but the retained Console audit "
            f"does not prove that it was performed ({automatic_rt_evidence.get('reason', 'unknown')})."
        )
    left_uncorrected = int(
        (automatic_rt_evidence.get("model_sources") or {}).get("Uncorrected", 0)
    )
    if automatic_rt_evidence.get("performed") and left_uncorrected:
        provenance_warnings.append(
            f"Automatic alignment RT correction left {left_uncorrected} file(s) uncorrected, so "
            "aligned retention times, mzTab-M included, contain measured retention times wherever "
            "those files contribute a peak."
        )
    methods = _methods_text(
        report_workflow, qa_report, qa_assessment, app_version, console_version
    )
    results = _results_text(qa_report, qa_assessment)
    rows = supplementary_rows(
        report_workflow,
        qa_report,
        qa_assessment,
        app_version=app_version,
        console_version=console_version,
    )

    methods_path = root / "MS_DIAL_Materials_and_Methods.txt"
    results_path = root / "MS_DIAL_QA_Results.txt"
    table_path = root / "Supplementary_Table_MS_DIAL.tsv"
    workbook_path = root / "Supplementary_Table_MS_DIAL.xlsx"
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
    write_supplementary_workbook(
        workbook_path,
        report_workflow,
        qa_report,
        qa_assessment,
        app_version=app_version,
        console_version=console_version,
    )
    audit = {
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "software": {
            "msdial_console_version": console_version or "not recorded",
            "msdial_interactive_version": app_version,
        },
        "qa_assessment": qa_assessment,
        "library_provenance_warnings": provenance_warnings,
        "workflow": report_workflow,
        "qa_report": qa_report,
    }
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (methods_path, results_path, workbook_path, table_path, audit_path):
            archive.write(path, path.name)
    return {
        "methods_text": methods,
        "qa_results_text": results,
        "qa_assessment": qa_assessment,
        "warnings": provenance_warnings,
        "methods_file": str(methods_path),
        "qa_results_file": str(results_path),
        "supplementary_workbook": str(workbook_path),
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
    summary = with_qc_minimum(summary) if qa_report and isinstance(summary, dict) else summary
    reasons = summary.get("not_assessed_reasons") if isinstance(summary, dict) else None
    reasons = reasons if isinstance(reasons, dict) else {}
    checks = [
        _check(summary, "median_qc_rsd_percent", "Median QC feature RSD", "<=", resolved["median_qc_rsd_percent_max"], "%"),
        _check(summary, "qc_features_rsd_le_30_percent", "Fraction of QC features with RSD <=30%", ">=", resolved["qc_features_rsd_le_30_fraction_min"], "fraction"),
        _check(summary, "median_qc_detection_rate", "Median QC detection rate", ">=", resolved["median_qc_detection_rate_min"], "fraction"),
        _check(summary, "sample_blank_ratio_ge_3", "Fraction of features with Sample/Blank >=3", ">=", resolved["sample_blank_ratio_ge_3_fraction_min"], "fraction"),
        _check(summary, "qc_pca_relative_dispersion", "QC PCA relative dispersion", "<=", resolved["qc_pca_relative_dispersion_max"], "ratio"),
        _check(summary, "median_blank_carryover_ratio", "Median blank carryover ratio", "<=", resolved["median_blank_carryover_ratio_max"], "fraction"),
        _check(summary, "run_order_intensity_correlation", "Absolute run-order/intensity correlation", "abs<=", resolved["run_order_intensity_abs_correlation_max"], "correlation"),
    ]
    fallback = "the QA matrix gives no value for it" if qa_report else "no LC-MS QA matrix was supplied"
    for item in checks:
        if item["status"] == "not_assessed":
            item["reason"] = str(reasons.get(item["metric"]) or fallback)
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
    excluded = {
        "files",
        "console_path",
        "template_path",
        "output_root",
        "automatic_rt_correction_evidence",
    } | annotation_keys
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

    automatic_rt_evidence = workflow.get("automatic_rt_correction_evidence") or {}
    if automatic_rt_evidence.get("requested"):
        for key in (
            "performed",
            "method_key_applied",
            "reference_file_id",
            "reference_file_name",
            "files_audited",
            "selected_anchor_count",
            "model_sources",
            "reason",
            "summary_file",
            "anchors_file",
            "method_keys_file",
        ):
            add(
                "Automatic alignment RT correction evidence",
                "Retained run evidence",
                key,
                automatic_rt_evidence.get(key),
                "Derived from retained Console outputs",
                "MS-DIAL Console audit",
            )

    if qa_report:
        for key, value in sorted((qa_report.get("summary") or {}).items()):
            if key == "not_assessed_reasons":
                continue  # each criterion's own row carries its reason
            add("Quality assurance", "Observed metric", key, value)
        for item in qa_assessment.get("checks", []):
            add("Quality assurance", item["label"], "Observed", item.get("value"), item.get("unit", ""))
            add("Quality assurance", item["label"], "Criterion", f"{item['operator']} {item['threshold']}", item.get("unit", ""))
            add("Quality assurance", item["label"], "Assessment", item["status"])
            if item.get("status") == "not_assessed" and item.get("reason"):
                add("Quality assurance", item["label"], "Not assessed because", item["reason"])
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
    automatic_rt_evidence = workflow.get("automatic_rt_correction_evidence") or {}
    if automatic_rt_evidence.get("performed"):
        reference = (
            automatic_rt_evidence.get("reference_file_name")
            or automatic_rt_evidence.get("reference_file_id")
        )
        sources = automatic_rt_evidence.get("model_sources") or {}
        blank_models = int(sources.get("InterpolatedBlank", 0)) + int(sources.get("NearestBlank", 0))
        uncorrected = int(sources.get("Uncorrected", 0))
        others = max(int(automatic_rt_evidence.get("files_audited", 0)) - 1, 0)
        # The counts say how much of the run the correction reached: "applied" alone read the
        # same for one corrected file in thirty as for all of them. They partition the files
        # other than the reference, whose model is the identity. The axis sentence describes
        # the Console of MsdialWorkbench#810: alignment keeps the corrected times, so aligned
        # RTs, mzTab-M included, are on the reference file's axis while .mdpeak and annotation
        # keep measured ones, and no Console output says so. A file left uncorrected joins
        # alignment at its measured RTs, so where one exists the axis claim holds only for
        # features it does not contribute to.
        if uncorrected:
            # The spot RT exported as retention_time_in_seconds is the mean of the detected
            # peaks' apex RTs; start and end are the earliest and latest single apex RTs.
            axis = (
                f"Peaks from the {uncorrected} file(s) that kept their original retention times "
                "enter alignment at their measured retention times. An aligned feature "
                "retention time, exported as the mzTab-M retention_time_in_seconds, is the mean "
                "of the contributing peaks' apex retention times: it is on the retention-time "
                f"axis of reference file {reference} only where none of those files contributes, "
                "includes their measured retention times otherwise, and is wholly measured for "
                "a feature detected only in them. Its start and end are the earliest and latest "
                "single apex retention times, either of which can be a measured one"
            )
        else:
            axis = (
                "Aligned feature retention times, including those exported in mzTab-M, are "
                f"therefore on the retention-time axis of reference file {reference}"
            )
        paragraphs.append(
            "After peak detection and annotation on the original retention-time axis, "
            "MS-DIAL learned distributed anchor features and applied file-specific "
            "piecewise-linear retention-time correction during alignment only. The retained "
            f"Console audit records reference file {reference}, which defines the axis and "
            f"keeps its measured retention times. Of the other {others} audited file(s), "
            f"{automatic_rt_evidence.get('corrected_file_count', 0)} were corrected from their "
            f"own anchors ({automatic_rt_evidence.get('selected_anchor_count', 0)} distinct "
            f"anchor(s) used), {blank_models} Blank file(s) took an interpolated or "
            f"nearest-sample model, and {uncorrected} kept their original retention times. "
            f"{axis}; per-file peak lists and annotation retention-time evidence keep the "
            "measured retention times. The per-file models and anchor evidence are documented "
            "in Supplementary Table S1 and the retained automatic RT-correction TSV files."
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
    for path, item in _matched_library_provenance(workflow):
        if not item:
            continue
        identifier = item.get("doi") or item.get("record_url") or item.get("source")
        if identifier:
            name = item.get("label") or item.get("filename") or Path(path).name or "library"
            cited.append(f"{name} ({identifier})")
    cited = list(dict.fromkeys(cited))
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
        f"{counts.get('Blank', 0)} {_plural(counts.get('Blank', 0), 'blank')})."
    )
    # The battery was recited in full whatever the sample types allowed, so a run with no QC
    # and no Blank read as having had QC precision and blank separation assessed. Name what
    # was evaluated and what could not be, and why.
    return lead + " " + _qa_criteria_sentence(assessment, counts)


def _qa_criteria_sentence(assessment: dict[str, Any], counts: dict[str, Any]) -> str:
    checks = [item for item in assessment.get("checks", []) if isinstance(item, dict)]
    evaluated = [item["label"] for item in checks if item.get("status") != "not_assessed"]
    missing = [item["label"] for item in checks if item.get("status") == "not_assessed"]
    parts = []
    if evaluated:
        parts.append(
            f"Of {len(checks)} prespecified QA criteria, {len(evaluated)} could be evaluated "
            f"({'; '.join(evaluated)}), and {assessment.get('passed', 0)} of them "
            f"{'was' if assessment.get('passed', 0) == 1 else 'were'} met."
        )
    else:
        parts.append(f"None of the {len(checks)} prespecified QA criteria could be evaluated.")
    if missing:
        # Each criterion with the reason it fell to, grouped: "A and B because the run had 0 QC
        # injection(s), and at least three are needed; C because the run had no Blank files". The
        # reasons are fixed phrases, so the public reanalysis gate can compare them with the counts.
        groups: dict[str, list[str]] = {}
        for item in checks:
            if item.get("status") == "not_assessed":
                reason = str(item.get("reason") or "the QA matrix gives no value for it")
                groups.setdefault(reason, []).append(item["label"])
        listing = "; ".join(missing)
        if len(groups) == 1:
            parts.append(f"The other {len(missing)} ({listing}) could not be assessed because {next(iter(groups))}.")
        else:
            clauses = "; ".join(f"{_and_list(labels)} because {reason}" for reason, labels in groups.items())
            parts.append(f"The other {len(missing)} ({listing}) could not be assessed: {clauses}.")
    parts.append("Individual criteria and outcomes are reported in Supplementary Table S1.")
    return " ".join(parts)


def _and_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


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
    pieces.append(_qa_criteria_sentence(assessment, summary.get("category_counts", {})))
    failed = [item["label"] for item in assessment["checks"] if item["status"] == "fail"]
    if failed:
        pieces.append("Criteria requiring review were: " + "; ".join(failed) + ".")
    return " ".join(pieces)


def _library_warnings(workflow: dict[str, Any]) -> list[str]:
    warnings = []
    for path, item in _matched_library_provenance(workflow):
        # The warning asks for a version, DOI, repository URL or checksum, so any of them is
        # one; the guided workflow records the repository URL as "source".
        if not item or not any(
            str(item.get(key) or "").strip()
            for key in ("doi", "record_url", "source", "version", "sha256", "md5", "checksum")
        ):
            warnings.append(
                f"No persistent identifier was recorded for {Path(path).name}. Add a database version, DOI, repository URL, or checksum before publication."
            )
    return warnings


def _automatic_rt_correction_evidence(
    root: Path, workflow: dict[str, Any]
) -> dict[str, Any]:
    """Read proof of an executed automatic RT correction from retained run artifacts.

    The judgment is automatic_rt_evidence.automatic_rt_correction_proof, the one the audit
    viewer also shows, so the viewer cannot call a run verified that this report refuses to
    describe. Only the request comes from the workflow.
    """
    if not workflow.get("execute_automatic_rt_correction"):
        return {"requested": False, **unproven(), "reason": "not_requested"}
    return {"requested": True, **automatic_rt_correction_proof(root)}


def _matched_library_provenance(
    workflow: dict[str, Any]
) -> list[tuple[str, dict[str, Any] | None]]:
    # The guided workflow records a library under "path"; the library catalog uses
    # "local_path". Matching only the second made every agent-run library look unrecorded, so
    # the report warned that no identifier existed for libraries whose DOI it held.
    provenance = [item for item in workflow.get("library_provenance", []) if isinstance(item, dict)]

    def recorded_path(item: dict[str, Any]) -> str:
        return str(item.get("local_path") or item.get("path") or "").strip()

    by_path = {
        _library_path_key(recorded_path(item)): item
        for item in provenance
        if recorded_path(item)
    }
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in provenance:
        name = str(item.get("filename") or Path(recorded_path(item)).name).casefold()
        if name:
            by_name.setdefault(name, []).append(item)

    matches: list[tuple[str, dict[str, Any] | None]] = []
    for path in _used_library_paths(workflow):
        item = by_path.get(_library_path_key(path))
        if item is None:
            same_name = by_name.get(Path(path).name.casefold(), [])
            if len(same_name) == 1:
                item = same_name[0]
        matches.append((path, item))
    return matches


def _used_library_paths(workflow: dict[str, Any]) -> list[str]:
    paths = [str(item.get("msp_file_path", "")).strip() for item in workflow.get("msp_annotators", [])]
    paths += [str(item.get("text_db_file_path", "")).strip() for item in workflow.get("text_annotators", [])]
    paths.append(str(workflow.get("lbm_path", "")).strip())
    return list(dict.fromkeys(path for path in paths if path))


def _library_path_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve()).casefold()
    except OSError:
        return text.replace("/", "\\").casefold()


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
