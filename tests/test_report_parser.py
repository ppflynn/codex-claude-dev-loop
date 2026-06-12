import copy
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.orchestrator.report_parser import ReportValidationError, validate_review_report


BASE_REPORT = {
    "status": "PASS",
    "reviewed_at": "2026-06-11T00:00:00Z",
    "summary": "ok",
    "findings": [],
}

FINDING = {
    "id": "P1-1",
    "severity": "P1",
    "file": "src/app.py",
    "line": 3,
    "description": "bug",
    "fix_suggestion": "fix it",
}


class ReportParserTests(unittest.TestCase):
    def test_pass_with_empty_findings_passes(self):
        self.assertEqual(validate_review_report(copy.deepcopy(BASE_REPORT))["status"], "PASS")

    def test_pass_with_findings_fails(self):
        report = copy.deepcopy(BASE_REPORT)
        report["findings"] = [copy.deepcopy(FINDING)]
        with self.assertRaises(ReportValidationError):
            validate_review_report(report)

    def test_needs_fix_requires_findings(self):
        report = copy.deepcopy(BASE_REPORT)
        report["status"] = "NEEDS_FIX"
        with self.assertRaises(ReportValidationError):
            validate_review_report(report)
        report["findings"] = [copy.deepcopy(FINDING)]
        self.assertEqual(validate_review_report(report)["status"], "NEEDS_FIX")

    def test_blocked_and_failed_pass(self):
        for status in ("BLOCKED", "FAILED"):
            report = copy.deepcopy(BASE_REPORT)
            report["status"] = status
            self.assertEqual(validate_review_report(report)["status"], status)

    def test_missing_and_invalid_fields_fail(self):
        for key in ("status", "reviewed_at", "findings"):
            report = copy.deepcopy(BASE_REPORT)
            del report[key]
            with self.assertRaises(ReportValidationError):
                validate_review_report(report)
        report = copy.deepcopy(BASE_REPORT)
        report["status"] = "FAIL"
        with self.assertRaises(ReportValidationError):
            validate_review_report(report)

    def test_finding_validation(self):
        report = copy.deepcopy(BASE_REPORT)
        report["status"] = "NEEDS_FIX"
        report["findings"] = [copy.deepcopy(FINDING)]

        for key in ("id", "severity", "file", "description"):
            bad = copy.deepcopy(report)
            del bad["findings"][0][key]
            with self.assertRaises(ReportValidationError):
                validate_review_report(bad)

        for file_value in ("C:/secret/app.py", "../app.py", ".env", "config/.env.local"):
            bad = copy.deepcopy(report)
            bad["findings"][0]["file"] = file_value
            with self.assertRaises(ReportValidationError):
                validate_review_report(bad)

        bad = copy.deepcopy(report)
        bad["findings"][0]["severity"] = "P4"
        with self.assertRaises(ReportValidationError):
            validate_review_report(bad)


if __name__ == "__main__":
    unittest.main()
