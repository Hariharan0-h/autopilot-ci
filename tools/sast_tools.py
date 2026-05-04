"""sast_tools.py — Static analysis tool wrappers."""

import json
import subprocess
from pathlib import Path
from schemas import Finding, Severity


KNOWN_VULNERABLE_PACKAGES: dict[str, str] = {
    "requests==2.18.0": "CVE-2023-32681: SSRF via crafted redirect URL",
    "requests==2.19.0": "CVE-2023-32681: SSRF via crafted redirect URL",
    "pillow==9.0.0": "CVE-2023-44271: Uncontrolled resource consumption",
    "cryptography==38.0.0": "CVE-2023-49083: NULL ptr dereference in PKCS12",
    "urllib3==1.26.4": "CVE-2021-33503: Infinite loop in chunked requests",
    "django==3.2.0": "CVE-2021-35042: SQL injection via QuerySet.order_by",
    "flask==1.0.0": "CVE-2018-1000656: Denial of service via large JSON",
    "pyyaml==5.1": "CVE-2020-14343: Remote code execution via yaml.load",
    "sqlalchemy==1.3.0": "CVE-2019-7164: SQL injection via column clauses",
    "paramiko==2.4.0": "CVE-2018-7750: Authentication bypass in transport",
}

_BANDIT_SEVERITY_MAP: dict[str, Severity] = {
    "HIGH": Severity.CRITICAL,
    "MEDIUM": Severity.HIGH,
    "LOW": Severity.MEDIUM,
}


def run_bandit(filepath: str) -> list[Finding]:
    """Run bandit on a single Python file and return a list of Findings.

    Parses bandit's JSON output into Finding objects.
    Severity mapping: HIGH→critical, MEDIUM→high, LOW→medium.
    Returns [] if bandit is not installed or file doesn't exist.

    Args:
        filepath: Absolute or relative path to a Python file.

    Returns:
        List of Finding objects for each bandit issue found.
    """
    if not Path(filepath).exists():
        return []

    try:
        result = subprocess.run(
            ["bandit", "-f", "json", "-q", filepath],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        return []

    # bandit exits with code 1 when issues are found — that's OK
    raw = result.stdout.strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    findings: list[Finding] = []
    for issue in data.get("results", []):
        sev_str = issue.get("issue_severity", "LOW").upper()
        severity = _BANDIT_SEVERITY_MAP.get(sev_str, Severity.LOW)
        findings.append(Finding(
            file=issue.get("filename", filepath),
            line=issue.get("line_number", 0),
            severity=severity,
            category=issue.get("test_id", "bandit").lower(),
            message=issue.get("issue_text", ""),
            snippet=issue.get("code", "").strip(),
            suggestion=(
                f"See CWE-{issue.get('issue_cwe', {}).get('id', '?')} — "
                f"{issue.get('more_info', '')}"
            ),
            auto_fixable=False,
        ))
    return findings


def check_requirements_cve(requirements_path: str) -> list[str]:
    """Read requirements.txt and return CVE strings for vulnerable packages.

    Compares each pinned package==version against KNOWN_VULNERABLE_PACKAGES.
    Format: "package==version (CVE-ID: description)"

    Args:
        requirements_path: Path to a requirements.txt file.

    Returns:
        List of CVE hit strings for any matched vulnerable packages.
    """
    path = Path(requirements_path)
    if not path.exists():
        return []

    hits: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Normalize to lowercase for matching
        key = line.lower()
        for vuln_pkg, cve_desc in KNOWN_VULNERABLE_PACKAGES.items():
            if key == vuln_pkg.lower():
                hits.append(f"{line} ({cve_desc})")
    return hits


def run_semgrep(filepath: str) -> list[Finding]:
    """Run semgrep with --config=p/python on a file and return Findings.

    Parses semgrep's JSON output into Finding objects.
    Returns [] if semgrep is not installed or file doesn't exist.

    Args:
        filepath: Absolute or relative path to a Python file.

    Returns:
        List of Finding objects for each semgrep match.
    """
    if not Path(filepath).exists():
        return []

    try:
        result = subprocess.run(
            ["semgrep", "--config=p/python", "--json", "--quiet", filepath],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        return []

    raw = result.stdout.strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    findings: list[Finding] = []
    for match in data.get("results", []):
        meta = match.get("extra", {})
        sev_str = meta.get("severity", "WARNING").upper()
        if sev_str == "ERROR":
            severity = Severity.HIGH
        elif sev_str == "WARNING":
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        findings.append(Finding(
            file=match.get("path", filepath),
            line=match.get("start", {}).get("line", 0),
            severity=severity,
            category=match.get("check_id", "semgrep").split(".")[-1],
            message=meta.get("message", ""),
            snippet=meta.get("lines", "").strip(),
            suggestion=meta.get("fix", ""),
            auto_fixable=bool(meta.get("fix")),
        ))
    return findings
