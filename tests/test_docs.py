"""Documentation rules: every Markdown file says it is not legal advice; no document claims that a
law or regulation is satisfied; no forbidden strings or account ids anywhere in tracked text."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "cdk.out", "build", "node_modules", ".pytest_cache", ".ruff_cache", "out", ".deploy"}


def _files(pattern: str) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", pattern], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
        return [ROOT / p for p in out if (ROOT / p).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob(pattern) if not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


MD = _files("*.md")
TEXT = [p for p in _files("*") if p.suffix in {".md", ".py", ".json", ".yml", ".yaml", ".toml", ".sh", ".js",
                                               ".html", ".css", ".txt", ".jsonl", ""} and p.name != "uv.lock"]

DISCLAIMER = re.compile(r"(does not constitute|nothing[^.]{0,40}constitutes|not|no) legal advice|不构成法律意见", re.I)
NEGATION = re.compile(r"(不|未|并非|无|no |not |never )[^。.\n]{0,8}$", re.I)
CLAIMS = [
    re.compile(r"\b(is|are|being|fully|now)\s+compliant\b", re.I),
    re.compile(r"\bcompl(ies|y|iant)\s+with\b", re.I),
    re.compile(r"\bin\s+compliance\s+with\b", re.I),
    re.compile(r"\b(meets|satisfies|fulfils|fulfills)\s+(the\s+)?(requirements\s+of\s+)?[A-Z]{3,}\b"),
    re.compile(r"符合[^。\n]{0,20}(法|规|条例|标准|要求)"),
    re.compile(r"(满足|达到)[^。\n]{0,12}(法规|法律|合规)"),
    re.compile(r"合规认证|已合规|完全合规"),
]
REGULATIONS = re.compile(r"\b(GDPR|CCPA|CPRA|PIPL|PIPEDA|HIPAA|APPI|LGPD|PDPA|PIPA|BIPA)\b")
FORBIDDEN = re.compile(r"/Users/|163\.com|"
                       r"employee account|员工账号|负责人|客户要求|补充材料|档位", re.I)
ACCOUNT = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
# Project-specific names that must never appear are kept out of the repository on purpose: listing them here
# would publish them. Put one regular expression per line in the git-ignored file .forbidden-terms to check them.
LOCAL_TERMS_FILE = ROOT / ".forbidden-terms"


def _local_terms() -> re.Pattern | None:
    if not LOCAL_TERMS_FILE.is_file():
        return None
    terms = [t.strip() for t in LOCAL_TERMS_FILE.read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    return re.compile("|".join(f"(?:{t})" for t in terms), re.I) if terms else None


def test_markdown_files_found():
    assert len(MD) >= 20


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def claims(text: str) -> list[str]:
    """Claims that are not negated ("不声称满足…", "makes no statement that …")."""
    found = []
    for pattern in CLAIMS:
        for m in pattern.finditer(text):
            if not NEGATION.search(text[max(0, m.start() - 12):m.start()]):
                found.append(m.group(0))
    return found


@pytest.mark.parametrize("path", [p for p in MD if p.name != "CLAUDE.md"], ids=lambda p: str(p.relative_to(ROOT)))
def test_every_markdown_has_legal_disclaimer(path):
    # CLAUDE.md is exempt: it must contain exactly "@AGENTS.md" (AGENTS.md carries the statement).
    assert DISCLAIMER.search(_flat(path)), "missing 'not legal advice' statement"


@pytest.mark.parametrize("path", MD, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_compliance_conclusions(path):
    text = _flat(path)
    assert claims(text) == [], f"compliance claim(s): {claims(text)}"
    assert not REGULATIONS.search(text), f"names a regulation: {REGULATIONS.search(text).group(0)}"


def test_claim_patterns_catch_examples():
    # negative control: the patterns must fire on typical claims
    samples = ["This system is compliant with local law.", "The kiosk complies with the rules.",
               "该方案符合个人信息保护法", "满足相关法规要求", "Fully compliant design"]
    for s in samples:
        assert claims(s), s
    for s in ("本项目不声称满足任何具体法规", "it does not comply with any rule by itself"):
        assert claims(s) == [], s
    assert REGULATIONS.search("GDPR")


@pytest.mark.parametrize("path", TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_forbidden_strings_or_account_ids(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.name != "test_docs.py":
        m = FORBIDDEN.search(text)
        assert not m, f"forbidden string {m.group(0)!r}"
    for n in ACCOUNT.findall(text):
        assert n == "111122223333", f"12-digit number {n}"


def test_local_forbidden_terms_are_ignored_by_git():
    out = subprocess.run(["git", "check-ignore", "-q", str(LOCAL_TERMS_FILE)], cwd=ROOT)
    assert out.returncode == 0, ".forbidden-terms must stay git-ignored"


@pytest.mark.parametrize("path", TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_local_forbidden_terms(path):
    pattern = _local_terms()
    if pattern is None:
        pytest.skip("no local .forbidden-terms file")
    m = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    assert not m, f"forbidden term at offset {m.start()}"


def test_required_files():
    for name in ("README.md", "README.zh-CN.md", "LICENSE", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
                 ".gitignore", "SECURITY-NOTES.md", "THREAT_MODEL.md", "VERIFICATION.md", "DEPLOYMENT-LOG.md",
                 "OPEN-QUESTIONS.md", "DECISIONS.md", "THIRD-PARTY-LICENSES", "CHANGELOG.md", "VERSION", "AGENTS.md",
                 "CLAUDE.md", "Makefile", "docs/prior-art.md", "docs/architecture.md", "docs/deployment-guide.md",
                 "docs/demo-script.md", "docs/handover.md", "docs/cleanup.md", "docs/standards-review.md",
                 "docs/compliance/data-flow.md", "docs/compliance/data-inventory.md",
                 "docs/compliance/retention-and-deletion.md", "docs/compliance/third-parties-and-cross-border.md",
                 "scripts/demo.sh", ".github/CODEOWNERS"):
        assert (ROOT / name).is_file(), name
    assert "MIT No Attribution" in (ROOT / "LICENSE").read_text()
    assert "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved." in (ROOT / "LICENSE").read_text()
    assert (ROOT / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
    assert (ROOT / "VERSION").read_text().strip() == "0.1.0"


def test_threat_model_is_draft_and_covers_required_threats():
    t = (ROOT / "THREAT_MODEL.md").read_text()
    assert "Status: DRAFT" in t
    for needle in ("leaks from storage", "forwarded or guessed", "hijacking", "prompt injection",
                   "model output", "Personal data in logs", "Incomplete deletion"):
        assert needle in t, needle


def test_readme_section_order():
    t = (ROOT / "README.md").read_text()
    order = ["Prototype, not for production", "## Architecture", "## Prerequisites and cost", "## Deploy",
             "## Use and verify", "## Troubleshooting", "## Security and limits", "## Clean up",
             "## Repository layout", "## Local testing", "## Contributing", "## Security\n", "## License"]
    pos = [t.index(s) for s in order]
    assert pos == sorted(pos)
    assert "Not removed by `cdk destroy` alone" in t


def test_demo_script_has_required_parts():
    t = (ROOT / "docs" / "demo-script.md").read_text()
    for needle in ("10 minutes", "30 minutes", "Preparation checklist", "One-command start and reset",
                   "Offline fallback", "After the demo", "If it fails"):
        assert needle in t
    for d in ("D1", "D2", "D3", "D4", "D5", "D6"):
        assert d in t


def test_compliance_templates_complete():
    flow = (ROOT / "docs" / "compliance" / "data-flow.md").read_text()
    assert "```mermaid" in flow
    cross = (ROOT / "docs" / "compliance" / "third-parties-and-cross-border.md").read_text()
    assert "Default: none" in cross
