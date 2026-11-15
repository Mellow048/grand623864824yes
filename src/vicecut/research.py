"""Research ledger, two-source fact verification, confidence taxonomy, and credits generator (<build> #2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .config import ProjectBrief


class ConfidenceLevel(str, Enum):
    OFFICIAL = "official"
    ON_RECORD_INTERVIEW = "on_record_interview"
    SEEN_BY_PREVIEWER = "seen_by_previewer"
    SINGLE_SOURCE = "single_source"
    SOURCES_DISAGREE = "sources_disagree"
    ESTIMATE = "estimate"
    UNKNOWN = "unknown"
    FALSE = "false"


CONFIDENCE_PILL_LABELS: dict[ConfidenceLevel, str | None] = {
    ConfidenceLevel.OFFICIAL: None,
    ConfidenceLevel.ON_RECORD_INTERVIEW: None,
    ConfidenceLevel.SEEN_BY_PREVIEWER: "PREVIEW",
    ConfidenceLevel.SINGLE_SOURCE: "REPORTED",
    ConfidenceLevel.SOURCES_DISAGREE: "DISPUTED",
    ConfidenceLevel.ESTIMATE: "ESTIMATE",
    ConfidenceLevel.UNKNOWN: "UNCONFIRMED",
    ConfidenceLevel.FALSE: "BUSTED",
}

REQUIRED_SPOKEN_CAVEATS: dict[ConfidenceLevel, list[str]] = {
    ConfidenceLevel.SINGLE_SOURCE: ["only ", "has reported", "reported it", "single report"],
    ConfidenceLevel.SOURCES_DISAGREE: ["don't agree", "do not agree", "disagree", "split on"],
    ConfidenceLevel.ESTIMATE: ["estimate", "estimated", "about ", "roughly"],
    ConfidenceLevel.UNKNOWN: ["nobody has shown", "no one has shown", "not confirmed", "we don't know", "unknown"],
    ConfidenceLevel.FALSE: ["busted", "not true", "false", "fake", "debunked"],
}


@dataclass
class FactEntry:
    """Single verified research fact backed by exact quoted source text."""

    id: str
    claim: str
    source_name: str
    source_url: str
    exact_quote: str
    date: str
    confidence: ConfidenceLevel
    second_source_name: str = ""
    second_source_url: str = ""
    source_tier: str = "outlet"  # "maker" | "interview" | "outlet"
    is_leak: bool = False
    is_debunk: bool = False
    notes: str = ""

    @property
    def is_confirmed(self) -> bool:
        """Official maker pages or facts confirmed by a second independent source."""
        if self.is_leak and not self.is_debunk:
            return False
        if not self.exact_quote.strip() or not self.source_url.strip():
            return False
        if self.confidence == ConfidenceLevel.OFFICIAL and self.source_tier == "maker":
            return True
        if self.confidence == ConfidenceLevel.FALSE and self.is_debunk:
            return True
        return bool(self.second_source_url.strip() and self.second_source_url != self.source_url)

    @property
    def pill_label(self) -> str | None:
        return CONFIDENCE_PILL_LABELS.get(self.confidence)


@dataclass
class ResearchAuditResult:
    passed: bool
    usable_fact_count: int
    required_fact_count: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ResearchLedger:
    topic: str
    research_date: str
    facts: list[FactEntry] = field(default_factory=list)

    def fact_by_id(self, fact_id: str) -> FactEntry | None:
        for f in self.facts:
            if f.id == fact_id:
                return f
        return None

    def usable_facts(self, blocked_sources: list[str] | None = None) -> list[FactEntry]:
        blocked = [b.lower() for b in (blocked_sources or [])]
        out: list[FactEntry] = []
        for f in self.facts:
            if f.is_leak and not f.is_debunk:
                continue
            if _is_blocked(f.source_name, f.source_url, blocked):
                continue
            if not f.exact_quote.strip() or not f.source_url.strip():
                continue
            out.append(f)
        return out

    def audit(self, brief: ProjectBrief) -> ResearchAuditResult:
        """Audit fact ledger against <build> #2 & <direction> bans."""
        errors: list[str] = []
        warnings: list[str] = []
        blocked = [b.lower() for b in brief.media.blocked_sources]

        seen_ids: set[str] = set()
        usable = 0
        for f in self.facts:
            if f.id in seen_ids:
                errors.append(f"Duplicate fact ID '{f.id}'.")
            seen_ids.add(f.id)

            if not f.exact_quote.strip():
                errors.append(f"[{f.id}] Missing exact_quote from source page (nothing from memory).")
            if not f.source_url.strip() or not f.source_url.startswith(("http://", "https://")):
                errors.append(f"[{f.id}] Invalid or missing source_url.")
            if not f.date.strip():
                errors.append(f"[{f.id}] Missing source publication date.")
            if f.is_leak and not f.is_debunk:
                errors.append(f"[{f.id}] Leak used as evidence is BANNED (only allowed if debunked as FALSE).")
            if _is_blocked(f.source_name, f.source_url, blocked):
                errors.append(f"[{f.id}] Uses blocked source '{f.source_name}' ({f.source_url}).")

            if not f.is_confirmed:
                if f.confidence in (ConfidenceLevel.OFFICIAL, ConfidenceLevel.ON_RECORD_INTERVIEW, ConfidenceLevel.SEEN_BY_PREVIEWER):
                    errors.append(
                        f"[{f.id}] Not confirmed against a second source. Check against a second source or drop it."
                    )
                else:
                    warnings.append(
                        f"[{f.id}] Single/uncertain source ({f.confidence.value}); must carry spoken caveat & pill."
                    )
            usable += 1

        required = brief.required_fact_count()
        usable_valid = len(self.usable_facts(brief.media.blocked_sources))
        if usable_valid < required:
            errors.append(
                f"Insufficient confirmed facts: have {usable_valid}, need at least {required} "
                f"(1 usable fact per 16s for {brief.preset_spec['target_duration_s']}s)."
            )

        return ResearchAuditResult(
            passed=len(errors) == 0,
            usable_fact_count=usable_valid,
            required_fact_count=required,
            errors=errors,
            warnings=warnings,
        )

    def export_sources_yaml(self, out_path: Path) -> None:
        """Deduplicated source list ordered by tier (maker -> interview -> outlet)."""
        tier_rank = {"maker": 0, "interview": 1, "outlet": 2}
        by_url: dict[str, dict[str, Any]] = {}
        for f in sorted(self.facts, key=lambda x: (tier_rank.get(x.source_tier, 9), x.date)):
            if f.source_url not in by_url:
                by_url[f.source_url] = {
                    "name": f.source_name,
                    "url": f.source_url,
                    "date": f.date,
                    "tier": f.source_tier,
                    "fact_ids": [],
                }
            by_url[f.source_url]["fact_ids"].append(f.id)
            if f.second_source_url and f.second_source_url not in by_url:
                by_url[f.second_source_url] = {
                    "name": f.second_source_name or urlparse(f.second_source_url).netloc,
                    "url": f.second_source_url,
                    "date": f.date,
                    "tier": "corroboration",
                    "fact_ids": [f.id],
                }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            yaml.safe_dump({"topic": self.topic, "research_date": self.research_date, "sources": list(by_url.values())}, sort_keys=False),
            encoding="utf-8",
        )

    def save(self, path: Path) -> None:
        raw_facts = []
        for f in self.facts:
            d = asdict(f)
            d["confidence"] = f.confidence.value
            raw_facts.append(d)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {"topic": self.topic, "research_date": self.research_date, "facts": raw_facts},
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "ResearchLedger":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        facts: list[FactEntry] = []
        for item in raw.get("facts", []):
            facts.append(
                FactEntry(
                    id=str(item["id"]),
                    claim=str(item["claim"]),
                    source_name=str(item["source_name"]),
                    source_url=str(item["source_url"]),
                    exact_quote=str(item.get("exact_quote", "")),
                    date=str(item.get("date", "")),
                    confidence=ConfidenceLevel(item.get("confidence", "official")),
                    second_source_name=str(item.get("second_source_name", "")),
                    second_source_url=str(item.get("second_source_url", "")),
                    source_tier=str(item.get("source_tier", "outlet")),
                    is_leak=bool(item.get("is_leak", False)),
                    is_debunk=bool(item.get("is_debunk", False)),
                    notes=str(item.get("notes", "")),
                )
            )
        return cls(
            topic=str(raw.get("topic", "")),
            research_date=str(raw.get("research_date", "")),
            facts=facts,
        )


def _is_blocked(source_name: str, source_url: str, blocked_lower: list[str]) -> bool:
    hay = f"{source_name} {source_url}".lower()
    return any(b and b in hay for b in blocked_lower)
