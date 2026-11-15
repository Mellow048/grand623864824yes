"""Tests for script parser and 28-rule automated script checker."""

from __future__ import annotations

from pathlib import Path

from vicecut.config import ProjectBrief
from vicecut.research import ResearchLedger
from vicecut.script_checker import check_script, count_syllables
from vicecut.script_parser import parse_script_text


def test_syllable_counter() -> None:
    assert count_syllables("buy") == 1
    assert count_syllables("game") == 1
    assert count_syllables("street") == 1
    assert count_syllables("Leonida") >= 3
    assert count_syllables("estimate") >= 3


def test_flagship_gta6_sample_passes_all_28_script_checks() -> None:
    sample_dir = Path(__file__).resolve().parent.parent / "examples" / "gta6-sample"
    brief = ProjectBrief.load(sample_dir / "project.yaml")
    ledger = ResearchLedger.load(sample_dir / "research" / "facts.yaml")
    script = parse_script_text((sample_dir / "script" / "script.md").read_text(encoding="utf-8"))

    report = check_script(script, brief, ledger=ledger)
    assert report.passed is True, f"Unexpected failures: {[c for c in report.checks if c.status == 'FAIL']}"
    assert all(c.status == "PASS" for c in report.checks)
    assert len(report.checks) >= 27


def test_script_checker_catches_banned_phrases_and_structural_violations() -> None:
    bad_md = """# Bad Explainer

## Intro
Hey guys, in today's video let's dive in to GTA 6!
We are going to look at some stuff we think is cool and we love it.
You will hear a secret teaser here. [TEASER:T1] [TEASER:T2]
This handoff has way too many unnecessary words to pass the strict eight word cap. [HANDOFF]

## Section 1: Chaos [CHAPTER:CHAOS]
Remember T1? [PAYOFF:T1]
Remember T2 too? [PAYOFF:T2]
A joke line one. [JOKE]
A joke line two right after. [JOKE]
Digital Foundry says 200GB is locked in forever. [FACT:F05]

## Close
Thanks for watching everyone!
"""
    sample_dir = Path(__file__).resolve().parent.parent / "examples" / "gta6-sample"
    brief = ProjectBrief.load(sample_dir / "project.yaml")
    ledger = ResearchLedger.load(sample_dir / "research" / "facts.yaml")
    parsed = parse_script_text(bad_md)
    report = check_script(parsed, brief, ledger=ledger)

    failed_rules = {c.rule for c in report.checks if c.status == "FAIL"}
    assert report.passed is False
    assert "Banned phrases" in failed_rules
    assert "Intro handoff length" in failed_rules
    assert "Max 1 payoff per section" in failed_rules
    assert "No consecutive jokes" in failed_rules
    assert "Non-official confidence caveats" in failed_rules
    assert "'We' avoidance" in failed_rules
    assert "Advice motif repetition" in failed_rules
