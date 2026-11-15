#!/usr/bin/env python3
"""Triage one request issue: read the form fields, label, link duplicates, check the datasheet link.

Python standard library only. The issue text is untrusted data: it is read from the event file, parsed into
fields, validated against strict patterns and never passed to a shell, never interpolated into a command and
never followed as a link. Comments are built from fixed text plus validated values (part numbers that matched
PART_RE, issue numbers and URLs returned by the GitHub API), so nothing from the issue body is echoed.

Usage:
    triage.py --event EVENT.json            run against the GitHub API (needs GITHUB_TOKEN)
    triage.py --event EVENT.json --dry-run  print the planned actions, change nothing

Environment: GITHUB_TOKEN, GITHUB_REPOSITORY (owner/name), GITHUB_REPOSITORY_OWNER, GITHUB_API_URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

HERE = Path(__file__).resolve().parent
VENDORS_FILE = HERE / "vendors.txt"

MARKER = "<!-- driver-requests-triage -->"
NO_RESPONSE = "_No response_"
MAX_BODY = 65536          # GitHub's own issue body limit; anything longer is not a form
MAX_FIELD = 4000

# Form kind, keyed by the label each form sets.
KIND_DRIVER = "driver-request"
KIND_PORT = "port-request"
KIND_ERRATUM = "erratum"

# Heading (the form field's label) to field id, per form. tests/ checks these against the YAML files.
FORM_FIELDS: dict[str, dict[str, str]] = {
    KIND_DRIVER: {
        "Part number": "part",
        "Vendor": "vendor",
        "Bus": "bus",
        "Datasheet link": "datasheet",
        "Variants and packages": "variants",
        "Features you need first": "features",
        "Target MCU families": "targets",
        "Contribution": "contribution",
        "Confirmation": "confirm",
    },
    KIND_PORT: {
        "MCU family or target": "target",
        "SDK and version": "sdk",
        "RTOS": "rtos",
        "Development kit": "board",
        "Drivers you want on it": "drivers",
        "Why the existing ports do not fit": "why",
        "Contribution": "contribution",
        "Confirmation": "confirm",
    },
    KIND_ERRATUM: {
        "Driver repository": "driver",
        "Part number": "part",
        "Document number": "document",
        "Document revision": "revision",
        "Page, section and table or figure": "location",
        "What the document says": "says",
        "What the chip does": "observed",
        "Seen on hardware": "hardware",
        "Vendor errata link": "vendor-erratum",
        "Confirmation": "confirm",
    },
}

# Labels this workflow owns: it adds and removes them; maintainers own every other label.
LABEL_NEEDS_DATASHEET = "needs-datasheet"
LABEL_NEEDS_INFO = "needs-info"
MANAGED_FLAGS = (LABEL_NEEDS_DATASHEET, LABEL_NEEDS_INFO)
CHIP_PREFIX = "chip:"

# A normalised part number: starts with a letter or digit, then letters, digits, dot, dash, underscore.
PART_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,31}$")
REPO_RE = re.compile(r"^driver-[a-z0-9][a-z0-9-]{0,62}$")

# Suffixes that name a package, a temperature grade or a reel, not a different chip. Removed from the end.
_SUFFIX_RE = re.compile(
    r"(?:[-_ ](?:TR|T&R|REEL|R7|R13|T|TAPE|CT|ND|PBF|NOPB|Q1|SL|E3)|/[A-Z0-9#&]*|#[A-Z0-9]*)+$"
)
# An uploaded file (PDF, archive, document). Images (`user-attachments/assets`) are allowed: an erratum may need a
# capture the reporter made.
_ATTACHMENT_RE = re.compile(
    r"https://github\.com/(?:user-attachments/files|[^/\s]+/[^/\s]+/files)/", re.IGNORECASE
)
_HEADING_RE = re.compile(r"^### (.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^- \[([ xX])\] (.+?)\s*$")


# --------------------------------------------------------------------------------------------------------------
# Parsing and normalising (pure functions, unit tested)
# --------------------------------------------------------------------------------------------------------------

def parse_form(body: str | None, fields: dict[str, str]) -> dict[str, object]:
    """Split an issue-form body into {field id: value}.

    GitHub renders a form as one `### <label>` heading per field followed by the answer; an empty answer is
    `_No response_`; checkboxes are `- [X] <label>` lines. Unknown headings are ignored, so a heading typed
    inside an answer cannot create a field the form does not have; a repeated heading keeps the first value.
    Checkbox fields return {option label: bool}; every other field returns a stripped string ("" when empty).
    """
    result: dict[str, object] = {}
    if not body:
        return result
    text = body[:MAX_BODY].replace("\r\n", "\n").replace("\r", "\n")
    current: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if current is None or current in result:
            return
        result[current] = _field_value(lines)

    for line in text.split("\n"):
        m = _HEADING_RE.match(line)
        if m and m.group(1) in fields:
            flush()
            current = fields[m.group(1)]
            lines = []
        elif current is not None:
            lines.append(line)
    flush()
    return result


def _field_value(lines: list[str]) -> object:
    stripped = [ln for ln in lines if ln.strip()]
    boxes = [_CHECKBOX_RE.match(ln) for ln in stripped]
    if stripped and all(boxes):
        return {m.group(2): m.group(1) in "xX" for m in boxes if m}
    value = "\n".join(lines).strip()
    if value == NO_RESPONSE:
        return ""
    return value[:MAX_FIELD]


def normalise_part(raw: object) -> str | None:
    """Return the base part number in upper case, or None when the text is not a plausible part number.

    Takes the first token, upper-cases it, removes package, grade and reel suffixes (`-TR`, `/NOPB`, `#PBF`,
    `-Q1`, ...) and checks the result against PART_RE. Anything with shell, Markdown or HTML characters fails.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or "\n" in text or len(text) > 64:
        return None
    token = text.split()[0].upper()
    previous = None
    while previous != token:
        previous = token
        token = _SUFFIX_RE.sub("", token)
    token = token.rstrip("._-")
    return token if PART_RE.match(token) else None


def chip_label(part: str) -> str:
    return CHIP_PREFIX + part


def load_vendors(path: Path = VENDORS_FILE) -> list[str]:
    """Read the vendor domain list: one lower-case domain per line, `#` starts a comment."""
    domains = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip().lower()
        if entry:
            domains.append(entry)
    return domains


def check_datasheet_url(raw: object, vendors: Iterable[str]) -> str | None:
    """Return None when the link is acceptable, otherwise a short fixed reason.

    Accepted: one https URL, no credentials, default port, host equal to a vendor domain or a subdomain of
    one. The link is only parsed, never fetched.
    """
    if not isinstance(raw, str) or not raw.strip():
        return "no datasheet link"
    text = raw.strip()
    if any(ch.isspace() for ch in text) or len(text) > 2000:
        return "the field must hold one link only"
    if _ATTACHMENT_RE.search(text):
        return "an uploaded file instead of a link to the vendor's website"
    try:
        url = urllib.parse.urlsplit(text)
        port = url.port
    except ValueError:
        return "not a valid URL"
    if url.scheme != "https":
        return "not an https link"
    if url.username or url.password:
        return "a link with credentials"
    if port not in (None, 443):
        return "a link with a non-standard port"
    host = (url.hostname or "").rstrip(".").lower()
    if not host or re.fullmatch(r"[0-9.]+|\[?[0-9a-f:]+\]?", host):
        return "not a vendor domain"
    for domain in vendors:
        if host == domain or host.endswith("." + domain):
            return None
    return "not on a vendor domain in tools/triage/vendors.txt"


def has_attachment(body: str | None) -> bool:
    return bool(body) and bool(_ATTACHMENT_RE.search(body[:MAX_BODY]))


def repo_matches_part(repo_name: str, part: str) -> str | None:
    """Compare a `driver-*` repository with a part number: "exact", "family", "prefix" or None.

    A trailing `x` in the repository name stands for one variant character (driver-bno08x covers BNO085).
    """
    if not REPO_RE.match(repo_name):
        return None
    stem = re.sub(r"[^A-Z0-9]", "", repo_name[len("driver-"):].upper())
    flat = re.sub(r"[^A-Z0-9]", "", part)
    if not stem or not flat:
        return None
    if stem == flat:
        return "exact"
    if stem.endswith("X") and len(stem) > 2:
        pattern = re.escape(stem[:-1]) + "[A-Z0-9]"
        if re.fullmatch(pattern, flat) or re.match(pattern, flat):
            return "family"
    if len(stem) >= 4 and (flat.startswith(stem) or stem.startswith(flat)):
        return "prefix"
    return None


# --------------------------------------------------------------------------------------------------------------
# Decisions (pure, unit tested): what the issue should look like after triage
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    kind: str
    part: str | None = None
    add_labels: list[str] = field(default_factory=list)
    remove_labels: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def kind_of(labels: Iterable[str]) -> str | None:
    names = set(labels)
    for kind in (KIND_DRIVER, KIND_PORT, KIND_ERRATUM):
        if kind in names:
            return kind
    return None


def plan_issue(kind: str, body: str | None, labels: Iterable[str], vendors: list[str]) -> Plan:
    """Decide labels and notes for one issue from its body and current labels (no I/O)."""
    current = set(labels)
    fields = parse_form(body, FORM_FIELDS[kind])
    plan = Plan(kind=kind)
    flags: set[str] = set()

    if kind in (KIND_DRIVER, KIND_ERRATUM):
        plan.part = normalise_part(fields.get("part"))
        if plan.part is None:
            flags.add(LABEL_NEEDS_INFO)
            plan.notes.append(
                "The part number could not be read. Please edit the issue and give the base part number "
                "only: letters, digits, dot, dash and underscore (for example TMP117)."
            )
    if kind == KIND_DRIVER:
        reason = check_datasheet_url(fields.get("datasheet"), vendors)
        if reason is not None:
            flags.add(LABEL_NEEDS_DATASHEET)
            plan.notes.append(
                f"Datasheet link: {reason}. Please link the product page or datasheet on the vendor's own "
                "website (https). If the vendor is missing from `tools/triage/vendors.txt`, say so in a comment "
                "and a maintainer adds it."
            )
    if kind == KIND_ERRATUM:
        driver = fields.get("driver")
        if not (isinstance(driver, str) and REPO_RE.match(driver.strip().lower())):
            flags.add(LABEL_NEEDS_INFO)
            plan.notes.append("The driver repository should be a name like `driver-bmp581`.")
        link = fields.get("vendor-erratum")
        if isinstance(link, str) and link.strip():
            reason = check_datasheet_url(link, vendors)
            if reason is not None:
                plan.notes.append(f"Vendor errata link: {reason}.")
    if has_attachment(body):
        flags.add(LABEL_NEEDS_DATASHEET if kind == KIND_DRIVER else LABEL_NEEDS_INFO)
        plan.notes.append(
            "The issue contains an uploaded file. Please remove it and link the vendor's public page instead; "
            "documents under NDA or marked confidential cannot be used."
        )
    confirm = fields.get("confirm")
    if not (isinstance(confirm, dict) and confirm and all(confirm.values())):
        flags.add(LABEL_NEEDS_INFO)
        plan.notes.append("Please tick the confirmation boxes at the end of the form.")

    wanted = set(flags)
    if plan.part is not None:
        wanted.add(chip_label(plan.part))
    stale = {lb for lb in current if lb in MANAGED_FLAGS and lb not in flags}
    stale |= {lb for lb in current if lb.startswith(CHIP_PREFIX) and lb not in wanted}
    plan.add_labels = sorted(wanted - current)
    plan.remove_labels = sorted(stale)
    return plan


def build_comment(plan: Plan, duplicates: list[tuple[int, str, str]], drivers: list[tuple[str, str, str]],
                  erratum_repo: tuple[str, str] | None) -> str | None:
    """Return the triage comment, or None when there is nothing to say.

    `duplicates`: (number, state, html_url) of other issues with the same chip label.
    `drivers`: (repository name, match kind, html_url) of existing driver repositories.
    """
    parts: list[str] = []
    if plan.part is not None:
        parts.append(f"Part number read as `{plan.part}` (label `{chip_label(plan.part)}`).")
    if drivers:
        lines = ["", "Existing drivers that may cover it:"]
        for name, match, url in drivers:
            what = {"exact": "same part", "family": "same family", "prefix": "related part"}[match]
            lines.append(f"- [{name}]({url}) ({what})")
        parts.append("\n".join(lines))
    if duplicates:
        lines = ["", "Other requests for the same part (a maintainer closes duplicates; add a 👍 to the one kept):"]
        for number, state, url in duplicates:
            lines.append(f"- [#{number}]({url}) ({state})")
        parts.append("\n".join(lines))
    if erratum_repo is not None:
        name, url = erratum_repo
        parts.append(f"\nA maintainer moves this erratum to [{name}]({url}) after checking it.")
    if plan.notes:
        parts.append("\n" + "\n".join(f"- {n}" for n in plan.notes))
    if not drivers and not duplicates and not plan.notes and erratum_repo is None:
        return None
    return MARKER + "\n" + "\n".join(parts).strip() + "\n\nThis comment is updated when the issue is edited."


# --------------------------------------------------------------------------------------------------------------
# GitHub REST client (urllib, fixed host, token from the environment)
# --------------------------------------------------------------------------------------------------------------

class GitHub:
    def __init__(self, token: str, api: str = "https://api.github.com", dry_run: bool = False,
                 log: Callable[[str], None] = print) -> None:
        if not api.startswith("https://"):
            raise ValueError("GITHUB_API_URL must be https")
        self._token = token
        self._api = api.rstrip("/")
        self.dry_run = dry_run
        self._log = log

    def _request(self, method: str, path: str, payload: object | None = None) -> object:
        url = self._api + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self._token:
            req.add_header("Authorization", "Bearer " + self._token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https API host)
            raw = resp.read()
        return json.loads(raw) if raw else None

    def get(self, path: str) -> object:
        return self._request("GET", path)

    def write(self, method: str, path: str, payload: object | None = None) -> object:
        self._log(f"{'would ' if self.dry_run else ''}{method} {path}")
        if self.dry_run:
            if payload is not None:
                self._log(json.dumps(payload, indent=2, ensure_ascii=False))
            return None
        return self._request(method, path, payload)

    # Reads -------------------------------------------------------------------------------------------------
    def issues_with_label(self, repo: str, label: str) -> list[dict]:
        query = urllib.parse.urlencode({"labels": label, "state": "all", "per_page": 100})
        items = self.get(f"/repos/{repo}/issues?{query}") or []
        return [i for i in items if isinstance(i, dict) and "pull_request" not in i]

    def org_repos(self, org: str) -> list[dict]:
        repos: list[dict] = []
        for page in range(1, 11):
            query = urllib.parse.urlencode({"type": "public", "per_page": 100, "page": page})
            batch = self.get(f"/orgs/{urllib.parse.quote(org)}/repos?{query}") or []
            repos.extend(r for r in batch if isinstance(r, dict))
            if len(batch) < 100:
                break
        return repos

    def comments(self, repo: str, number: int) -> list[dict]:
        return self.get(f"/repos/{repo}/issues/{number}/comments?per_page=100") or []

    # Writes ------------------------------------------------------------------------------------------------
    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self.write("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": labels})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        try:
            self.write("DELETE", f"/repos/{repo}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}")
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise

    def upsert_comment(self, repo: str, number: int, body: str | None) -> None:
        mine = [c for c in self.comments(repo, number)
                if isinstance(c, dict) and MARKER in str(c.get("body", ""))
                and str((c.get("user") or {}).get("type", "")) == "Bot"]
        if mine:
            if body is None:
                return
            if mine[0].get("body") != body:
                self.write("PATCH", f"/repos/{repo}/issues/comments/{int(mine[0]['id'])}", {"body": body})
        elif body is not None:
            self.write("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})


# --------------------------------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------------------------------

def triage(event: dict, gh: GitHub, repo: str, org: str, vendors: list[str],
           log: Callable[[str], None] = print) -> Plan | None:
    issue = event.get("issue")
    if not isinstance(issue, dict) or "pull_request" in issue or issue.get("state") != "open":
        log("not an open issue: nothing to do")
        return None
    number = int(issue["number"])
    labels = [str(lb.get("name", "")) for lb in issue.get("labels", []) if isinstance(lb, dict)]
    kind = kind_of(labels)
    if kind is None:
        log(f"#{number}: not a request form: nothing to do")
        return None
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    plan = plan_issue(kind, body, labels, vendors)
    log(f"#{number}: {kind}, part {plan.part or '-'}, add {plan.add_labels}, remove {plan.remove_labels}")

    for label in plan.remove_labels:
        gh.remove_label(repo, number, label)
    if plan.add_labels:
        gh.add_labels(repo, number, plan.add_labels)

    duplicates: list[tuple[int, str, str]] = []
    drivers: list[tuple[str, str, str]] = []
    erratum_repo: tuple[str, str] | None = None
    if plan.part is not None:
        for other in gh.issues_with_label(repo, chip_label(plan.part)):
            if int(other.get("number", 0)) != number:
                duplicates.append((int(other["number"]), str(other.get("state", "")),
                                   str(other.get("html_url", ""))))
        duplicates.sort()
    if plan.part is not None or kind == KIND_ERRATUM:
        repos = gh.org_repos(org)
        fields = parse_form(body, FORM_FIELDS[kind])
        named = str(fields.get("driver", "")).strip().lower()
        for r in repos:
            name = str(r.get("name", ""))
            url = str(r.get("html_url", ""))
            if kind == KIND_ERRATUM and REPO_RE.match(named) and name == named:
                erratum_repo = (name, url)
            match = repo_matches_part(name, plan.part) if plan.part else None
            if match is not None and kind == KIND_DRIVER:
                drivers.append((name, match, url))
        drivers.sort()
    gh.upsert_comment(repo, number, build_comment(plan, duplicates, drivers, erratum_repo))
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--event", required=True, type=Path, help="the issues event JSON (GITHUB_EVENT_PATH)")
    ap.add_argument("--dry-run", action="store_true", help="print planned writes, change nothing")
    args = ap.parse_args(argv)

    repo = os.environ.get("GITHUB_REPOSITORY", "kokeb-labs-drivers/driver-requests")
    org = os.environ.get("GITHUB_REPOSITORY_OWNER", repo.split("/", 1)[0])
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo) or not re.fullmatch(r"[A-Za-z0-9-]+", org):
        print("GITHUB_REPOSITORY or GITHUB_REPOSITORY_OWNER is malformed", file=sys.stderr)
        return 2
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and not args.dry_run:
        print("GITHUB_TOKEN is not set (use --dry-run to try locally)", file=sys.stderr)
        return 2
    event = json.loads(args.event.read_text(encoding="utf-8"))
    gh = GitHub(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"), dry_run=args.dry_run)
    triage(event, gh, repo, org, load_vendors())
    return 0


if __name__ == "__main__":
    sys.exit(main())
