"""Tests for research fact ledger verification and voice-only TTS pronunciation respelling."""

from __future__ import annotations

from pathlib import Path

from vicecut.config import ProjectBrief
from vicecut.pronunciation import PronunciationLexicon, int_to_spoken, year_to_spoken
from vicecut.research import ConfidenceLevel, FactEntry, ResearchLedger


def _make_fact(**overrides) -> FactEntry:
    base = dict(
        id="F01",
        claim="GTA 6 launches on PS5 and Xbox Series X|S.",
        source_name="Rockstar Games",
        source_url="https://www.rockstargames.com/VI",
        exact_quote="Coming to PlayStation 5 and Xbox Series X|S.",
        date="2025-05-06",
        confidence=ConfidenceLevel.OFFICIAL,
        second_source_name="Take-Two Interactive",
        second_source_url="https://www.take2games.com/",
        source_tier="maker",
        is_leak=False,
        is_debunk=False,
    )
    base.update(overrides)
    return FactEntry(**base)


def test_research_ledger_audit_enforces_two_sources_and_blocks_leaks(tmp_path: Path) -> None:
    brief = ProjectBrief(
        slug="gta6-test",
        topic="everything you need to know about GTA 6 before you buy",
        subject="GTA 6",
        action="buy",
        viewer="Console gamers",
        length_preset="sample",
    )
    good_facts = [_make_fact(id=f"F{i:02d}") for i in range(1, 6)]
    ledger = ResearchLedger(topic="GTA 6", research_date="2026-11-15", facts=good_facts)
    report = ledger.audit(brief)
    assert report.passed is True
    assert report.usable_fact_count == 5

    # Export tiered sources.yaml
    out = tmp_path / "sources.yaml"
    ledger.export_sources_yaml(out)
    assert out.exists() and "Rockstar Games" in out.read_text(encoding="utf-8")

    # Leak used as evidence (not debunk) MUST fail audit!
    leak_fact = _make_fact(id="F99", is_leak=True, is_debunk=False, confidence=ConfidenceLevel.SINGLE_SOURCE)
    bad_ledger = ResearchLedger(topic="GTA 6", research_date="2026-11-15", facts=good_facts + [leak_fact])
    bad_report = bad_ledger.audit(brief)
    assert bad_report.passed is False
    assert any("leak" in issue.lower() for issue in bad_report.errors)

    # Leak explicitly DEBUNKED as FALSE is allowed!
    debunk = _make_fact(id="F98", is_leak=True, is_debunk=True, confidence=ConfidenceLevel.FALSE)
    ok_debunk = ResearchLedger(topic="GTA 6", research_date="2026-11-15", facts=good_facts + [debunk])
    assert ok_debunk.audit(brief).passed is True

    # Unconfirmed outlet report without a 2nd independent source MUST fail!
    unconfirmed = _make_fact(
        id="F97",
        source_tier="outlet",
        confidence=ConfidenceLevel.SEEN_BY_PREVIEWER,
        second_source_name="",
        second_source_url="",
    )
    unc_ledger = ResearchLedger(topic="GTA 6", research_date="2026-11-15", facts=good_facts + [unconfirmed])
    unc_report = unc_ledger.audit(brief)
    assert unc_report.passed is False
    assert any("second source" in issue.lower() for issue in unc_report.errors)


def test_pronunciation_respeller_only_transforms_tts_speech() -> None:
    lex = PronunciationLexicon()
    raw = "GTA VI runs at 60fps on PS5 Pro in Leonida, costs $70, takes 200GB, and shipped in 2026 after $1.5B in spend."
    spoken = lex.to_spoken(raw)

    assert "G T A six" in spoken
    assert "sixty frames a second" in spoken
    assert "P S five Pro" in spoken
    assert "Lee-oh-nee-dah" in spoken
    assert "seventy bucks" in spoken
    assert "two hundred gigs" in spoken
    assert "twenty twenty-six" in spoken
    assert "one point five billion dollars" in spoken
    # Original on-screen string untouched
    assert "$70" in raw and "200GB" in raw

    assert int_to_spoken(2025) == "two thousand and twenty-five"
    assert year_to_spoken(2026) == "twenty twenty-six"
    assert year_to_spoken(2005) == "two thousand and five"

    risky = lex.audit_risky_tokens("Rockstar uses RAGE and DLSS on PC.")
    assert "DLSS" in risky
    assert "PC" not in risky  # already covered by default overrides
