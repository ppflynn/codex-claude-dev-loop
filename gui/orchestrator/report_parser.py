from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .path_safety import PathSafetyError, validate_relative_project_path
from .state_machine import Status


class ReportValidationError(ValueError):
    pass


VALID_REVIEW_STATUSES = {Status.PASS, Status.NEEDS_FIX, Status.BLOCKED, Status.FAILED}
VALID_SEVERITIES = {"P0", "P1", "P2", "P3"}


def load_review_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReportValidationError(f"Review report does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportValidationError(f"Review report is not valid JSON: {exc}") from exc
    return validate_review_report(data)


def validate_review_report(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ReportValidationError("Review report must be a JSON object.")
    for field in ("status", "reviewed_at", "findings"):
        if field not in data:
            raise ReportValidationError(f"Review report is missing required field: {field}")

    status = data["status"]
    if status not in VALID_REVIEW_STATUSES:
        raise ReportValidationError(f"Invalid review status: {status}")

    reviewed_at = data["reviewed_at"]
    if not isinstance(reviewed_at, str) or not reviewed_at.strip():
        raise ReportValidationError("reviewed_at must be a non-empty ISO-8601 string.")
    try:
        datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportValidationError("reviewed_at must be a valid ISO-8601 string.") from exc

    findings = data["findings"]
    if not isinstance(findings, list):
        raise ReportValidationError("findings must be an array.")
    if status == Status.PASS and findings:
        raise ReportValidationError("PASS review reports must not contain findings.")
    if status == Status.NEEDS_FIX and not findings:
        raise ReportValidationError("NEEDS_FIX review reports must contain at least one finding.")

    for index, finding in enumerate(findings):
        validate_finding(finding, index)
    return data


def validate_finding(finding: Any, index: int) -> None:
    if not isinstance(finding, dict):
        raise ReportValidationError(f"finding[{index}] must be an object.")
    for field in ("id", "severity", "file", "description"):
        if field not in finding:
            raise ReportValidationError(f"finding[{index}] is missing required field: {field}")
        if not isinstance(finding[field], str) or not finding[field].strip():
            raise ReportValidationError(f"finding[{index}].{field} must be a non-empty string.")
    if finding["severity"] not in VALID_SEVERITIES:
        raise ReportValidationError(f"finding[{index}].severity is invalid: {finding['severity']}")
    try:
        validate_relative_project_path(finding["file"])
    except PathSafetyError as exc:
        raise ReportValidationError(f"finding[{index}].file is invalid: {exc}") from exc
