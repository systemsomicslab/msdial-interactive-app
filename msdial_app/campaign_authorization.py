"""A recorded campaign approval, and the one check every entry point makes against it.

WHY THIS EXISTS. Every irreversible or expensive step of a repository reanalysis asks for an explicit
confirmation: the download, the Class decision, each MS-DIAL run, the raw-data deletion. That is right for
one unit reviewed in a conversation and impossible for a campaign of several hundred units running for
weeks. The decision that replaces it is one approval, given once by a person, of one campaign manifest
digest, covering named boundaries for named units under stated rules.

The designs for that campaign each invented their own "campaign mode" - an authorization file here, a
disk guard there, ledger rows somewhere else - five definitions of the same thing. This module is the one
definition: a record with schema ``msdial-campaign-authorization.v1`` and a single question asked of it,
``validate(unit_id, boundary)``.

WHAT IT NEVER DOES. It never stands in for a confirmation it was not asked about. With no record passed,
nothing changes anywhere: every entry point still asks for ``confirmed=true`` exactly as before. A record
that cannot be read, names another unit, does not cover the boundary, or has been revoked is a refusal,
not a fallback to the old path - a caller that passed an approval and had it silently ignored would
believe it had been checked. Boundaries 2 (official-library download) and 6 (a person's reading of the
gate's flagged sentences) can never be covered; a record that claims either is refused whole.

WHAT IT RECORDS. A validation returns a crossing record - which approval, which manifest digest, which
boundary, which unit, the sha256 of the authorization file as read - and the caller writes it into the
unit's own manifest, so each boundary a unit crossed under a campaign is an artifact instead of a
conversation fact.

Libraries are named by file name and sha256 only. A record that carries a library location is refused:
the production library is private, and its location must not travel into every unit's provenance.

The record, as the campaign runner writes it::

    {
      "schema": "msdial-campaign-authorization.v1",
      "approval_id": "...",
      "campaign_id": "...",
      "manifest_digest": "sha256:<64 hex>",
      "campaign_manifest_path": "<optional; verified against the digest when present>",
      "approved_by": "...",
      "approved_at": "<ISO 8601>",
      "statement": "<the person's words, verbatim>",
      "covers": [1, 3, 4, 5, "split"],
      "units": ["<analysis unit id>", "<part id>", ...],
      "raw_retention_policy": "keep" | "delete_after_validated_output",
      "libraries": [{"name": "<file name>", "sha256": "<64 hex>"}],
      "revoked_at": null
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "msdial-campaign-authorization.v1"
# The contract's confirmation boundaries a campaign approval may stand in for, plus the automatic split
# of a unit whose raw headers disagree. 2 and 6 are absent on purpose.
COVERABLE_BOUNDARIES: frozenset[int | str] = frozenset({1, 3, 4, 5, "split"})
NEVER_COVERED = {
    2: "an official-library download is never part of a campaign approval",
    6: "a person's reading of the sentences a gate check left for them is never an agent's",
}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Keys that would put a library's location into a record copied into every unit's provenance.
_LOCATION_KEYS = frozenset({"path", "location", "uri", "url", "file", "file_path", "directory"})
_DELETE_RETENTION = "delete_after_validated_output"
_RETENTION_POLICIES = ("keep", _DELETE_RETENTION)


class CampaignAuthorizationError(ValueError):
    """A campaign approval was passed and does not cover what was asked of it.

    A ValueError, so every existing structured-error path reports it rather than raising past the MCP
    boundary; the message starts with a fixed token and the codes, so an unattended caller can tell a
    refused approval from any other validation failure without parsing prose.
    """

    def __init__(self, codes: list[str], reasons: list[str]) -> None:
        self.codes = list(dict.fromkeys(codes))
        self.reasons = list(reasons)
        super().__init__(
            f"campaign_authorization_refused [{', '.join(self.codes)}]: " + " ".join(self.reasons)
        )


def normalize_boundary(value: Any) -> int | str:
    """1, "1", 5 and "split" mean what they say; anything else is not a boundary."""
    text = str(value).strip().casefold()
    if text == "split":
        return "split"
    if text.isdecimal():
        return int(text)
    raise ValueError(f"Not a confirmation boundary: {value!r}")


@dataclass(frozen=True)
class CampaignAuthorization:
    path: Path
    sha256: str
    record: dict[str, Any]
    approval_id: str
    campaign_id: str
    manifest_digest: str
    covers: frozenset[int | str]
    units: frozenset[str]
    raw_retention_policy: str
    libraries: tuple[dict[str, str], ...] = field(default_factory=tuple)
    revoked_at: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "CampaignAuthorization":
        """Read and check the record itself. Refuses the whole record on any defect."""
        location = Path(str(path)).expanduser()
        try:
            data = location.read_bytes()
            record = json.loads(data.decode("utf-8-sig"))
        except (OSError, ValueError) as error:
            raise CampaignAuthorizationError(
                ["unreadable"], [f"The campaign authorization {location} could not be read: {error}."]
            ) from error
        if not isinstance(record, dict):
            raise CampaignAuthorizationError(["malformed"], ["A campaign authorization is one JSON object."])

        codes: list[str] = []
        reasons: list[str] = []

        def refuse(code: str, reason: str) -> None:
            codes.append(code)
            reasons.append(reason)

        if record.get("schema") != SCHEMA:
            refuse("schema", f"The record's schema is {record.get('schema')!r}, not {SCHEMA!r}.")
        text = {key: str(record.get(key) or "").strip() for key in (
            "approval_id", "campaign_id", "manifest_digest", "approved_by", "approved_at",
        )}
        for key in ("approval_id", "approved_by", "approved_at"):
            if not text[key]:
                refuse("incomplete", f"The record names no {key}.")
        if not _DIGEST.fullmatch(text["manifest_digest"]):
            refuse(
                "manifest_digest",
                "The record's manifest_digest is not 'sha256:' followed by 64 lowercase hex digits.",
            )

        covers: set[int | str] = set()
        for item in record.get("covers") or []:
            try:
                boundary = normalize_boundary(item)
            except ValueError:
                refuse("covers", f"The record claims to cover {item!r}, which is not a boundary.")
                continue
            if boundary in NEVER_COVERED:
                refuse(f"covers_{boundary}", f"The record claims boundary {boundary}, but {NEVER_COVERED[boundary]}.")
            elif boundary not in COVERABLE_BOUNDARIES:
                refuse("covers", f"The record claims boundary {boundary!r}, which no campaign approval covers.")
            else:
                covers.add(boundary)
        if not covers and "covers" not in codes:
            refuse("covers", "The record covers no boundary.")

        units = {str(item).strip() for item in record.get("units") or [] if str(item).strip()}
        if not units:
            refuse("units", "The record names no analysis unit.")

        retention = str(record.get("raw_retention_policy") or "").strip()
        if retention not in _RETENTION_POLICIES:
            refuse(
                "retention",
                f"The record's raw_retention_policy is {retention!r}; it must be one of {list(_RETENTION_POLICIES)}.",
            )

        libraries: list[dict[str, str]] = []
        for item in record.get("libraries") or []:
            if not isinstance(item, dict):
                refuse("libraries", "Each library is an object with a name and a sha256.")
                continue
            if _LOCATION_KEYS & {str(key).casefold() for key in item}:
                refuse(
                    "library_location_recorded",
                    "A library entry carries a location. Name libraries by file name and sha256 only; "
                    "a private library's location must not be copied into unit provenance.",
                )
                continue
            name = str(item.get("name") or "").strip()
            digest = str(item.get("sha256") or "").strip().casefold()
            if not name or any(mark in name for mark in ("/", "\\", ":")):
                refuse(
                    "library_location_recorded" if name else "libraries",
                    f"A library name must be a bare file name, not {name!r}.",
                )
                continue
            if not _SHA256.fullmatch(digest):
                refuse("libraries", f"Library {name} has no 64-digit sha256.")
                continue
            libraries.append({"name": name, "sha256": digest})

        manifest_path = str(record.get("campaign_manifest_path") or "").strip()
        if manifest_path and "manifest_digest" not in codes:
            # Optional, and checked when present: the approval is of a digest, and a named manifest that
            # no longer hashes to it is a different campaign from the one approved.
            try:
                actual = "sha256:" + hashlib.sha256(Path(manifest_path).expanduser().read_bytes()).hexdigest()
            except OSError as error:
                refuse("campaign_manifest_unreadable", f"The campaign manifest could not be read: {error}.")
            else:
                if actual != text["manifest_digest"]:
                    refuse(
                        "manifest_digest_mismatch",
                        f"The campaign manifest hashes to {actual}, not the approved {text['manifest_digest']}.",
                    )

        if codes:
            raise CampaignAuthorizationError(codes, reasons)
        return cls(
            path=location.resolve(),
            sha256=hashlib.sha256(data).hexdigest(),
            record=record,
            approval_id=text["approval_id"],
            campaign_id=text["campaign_id"],
            manifest_digest=text["manifest_digest"],
            covers=frozenset(covers),
            units=frozenset(units),
            raw_retention_policy=retention,
            libraries=tuple(libraries),
            revoked_at=str(record.get("revoked_at") or "").strip(),
        )

    def check(
        self,
        unit_id: str,
        boundary: Any,
        *,
        parent_unit_id: str = "",
        raw_retention_policy: str | None = None,
    ) -> dict[str, Any]:
        """Whether this approval covers one boundary for one unit. Changes nothing, never raises."""
        codes: list[str] = []
        reasons: list[str] = []
        unit = str(unit_id or "").strip()
        parent = str(parent_unit_id or "").strip()
        try:
            wanted = normalize_boundary(boundary)
        except ValueError as error:
            return {"valid": False, "codes": ["boundary"], "reasons": [str(error)]}

        if self.revoked_at:
            codes.append("revoked")
            reasons.append(f"Approval {self.approval_id} was revoked at {self.revoked_at}.")
        if wanted in NEVER_COVERED:
            codes.append(f"boundary_{wanted}")
            reasons.append(f"Boundary {wanted} cannot be covered: {NEVER_COVERED[wanted]}.")
        elif wanted not in self.covers:
            codes.append("boundary_not_covered")
            reasons.append(f"Approval {self.approval_id} does not cover boundary {wanted}.")

        covered_as = ""
        if not unit:
            codes.append("unit_unnamed")
            reasons.append("The step names no analysis unit, so no campaign approval can cover it.")
        elif unit in self.units:
            covered_as = "listed"
        elif (
            parent
            and parent in self.units
            and unit.startswith(parent + "-")
            and "split" in self.covers
        ):
            # A part is named after its parent and exists only because the approved split made it. Its
            # identity is taken from the part's own manifest (split_from), never from the id alone.
            covered_as = f"derived_part_of:{parent}"
        else:
            codes.append("unit_not_covered")
            reasons.append(f"Approval {self.approval_id} does not name analysis unit {unit}.")

        if raw_retention_policy is not None and str(raw_retention_policy).strip() != self.raw_retention_policy:
            codes.append("retention_mismatch")
            reasons.append(
                f"The step asks for raw retention {str(raw_retention_policy).strip()!r}; the approval "
                f"states {self.raw_retention_policy!r}."
            )
        if wanted == 5 and self.raw_retention_policy != _DELETE_RETENTION:
            codes.append("retention_keep")
            reasons.append(
                f"Approval {self.approval_id} keeps raw data ({self.raw_retention_policy!r}); it cannot "
                "stand in for a deletion."
            )
        return {
            "valid": not codes,
            "codes": codes,
            "reasons": reasons,
            "boundary": wanted,
            "unit_id": unit,
            "covered_as": covered_as,
        }

    def validate(
        self,
        unit_id: str,
        boundary: Any,
        *,
        entry_point: str = "",
        parent_unit_id: str = "",
        raw_retention_policy: str | None = None,
    ) -> dict[str, Any]:
        """The crossing record for a covered boundary, or CampaignAuthorizationError."""
        verdict = self.check(
            unit_id, boundary, parent_unit_id=parent_unit_id, raw_retention_policy=raw_retention_policy
        )
        if not verdict["valid"]:
            raise CampaignAuthorizationError(verdict["codes"], verdict["reasons"])
        return {
            "schema": SCHEMA,
            "approval_id": self.approval_id,
            "campaign_id": self.campaign_id,
            "manifest_digest": self.manifest_digest,
            "authorization_path": str(self.path),
            "authorization_sha256": self.sha256,
            "raw_retention_policy": self.raw_retention_policy,
            "boundary": verdict["boundary"],
            "unit_id": verdict["unit_id"],
            "covered_as": verdict["covered_as"],
            "entry_point": str(entry_point or ""),
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }


def load_campaign_authorization(path: str | Path | None) -> CampaignAuthorization | None:
    """None when no record was passed, which is how every existing path stays exactly as it was."""
    if path is None or not str(path).strip():
        return None
    return CampaignAuthorization.load(path)


def authorize(
    path: str | Path | None,
    unit_id: str,
    boundary: Any,
    *,
    entry_point: str,
    parent_unit_id: str = "",
    raw_retention_policy: str | None = None,
) -> dict[str, Any] | None:
    """The one call an entry point makes.

    Returns None when no authorization was passed - the caller then asks for its confirmation exactly as
    before. Returns the crossing record to write into the unit manifest when the approval covers this
    boundary for this unit. Raises CampaignAuthorizationError otherwise, including when confirmed=true was
    also passed: an approval that was offered and does not hold is a refusal, never ignored.
    """
    authorization = load_campaign_authorization(path)
    if authorization is None:
        return None
    return authorization.validate(
        unit_id,
        boundary,
        entry_point=entry_point,
        parent_unit_id=parent_unit_id,
        raw_retention_policy=raw_retention_policy,
    )


def unit_identity(manifest: dict[str, Any]) -> tuple[str, str]:
    """(analysis unit id, parent unit id) as a unit's own manifest records them."""
    project = manifest.get("project") if isinstance(manifest, dict) else None
    unit = str((project or {}).get("analysis_unit_id") or "").strip()
    parent = str(((manifest or {}).get("split_from") or {}).get("analysis_unit_id") or "").strip()
    return unit, parent
