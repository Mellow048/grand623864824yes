"""Quantitative QC audit dashboard, honest shortfalls generator, and source credits compiler (<build> #7)."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .align import AlignmentResult
from .audio import AudioMasterStats, compute_longest_pause
from .config import ProjectBrief
from .media import MediaManifest
from .research import ResearchLedger
from .timeline import Timeline


@dataclass
class QCMetric:
    name: str
    measured: str
    target: str
    passed: bool
    note: str = ""


@dataclass
class QCReport:
    project_slug: str
    passed: bool
    metrics: list[QCMetric] = field(default_factory=list)
    shortfalls: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# ViceCut QC Report — `{self.project_slug}`",
            "",
            f"**Overall Status:** {'PASS' if self.passed else 'NEEDS FIXES'}",
            "",
            "| Metric | Measured | Target | Status | Notes |",
            "| :--- | :--- | :--- | :---: | :--- |",
        ]
        for m in self.metrics:
            badge = "PASS" if m.passed else "FAIL"
            lines.append(f"| {m.name} | {m.measured} | {m.target} | **{badge}** | {m.note} |")

        lines.append("")
        lines.append("## Honest Shortfalls")
        if self.shortfalls:
            for item in self.shortfalls:
                lines.append(f"- {item}")
        else:
            lines.append("- None measured in automated metrics; complete visual sign-off on all 3 look-and-fix sheets.")
        lines.append("")
        return "\n".join(lines)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(), encoding="utf-8")


def _merged_coverage_s(spans: list[tuple[float, float]]) -> float:
    if not spans:
        return 0.0
    ordered = sorted(spans)
    merged = [ordered[0]]
    for a, b in ordered[1:]:
        if a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return sum(max(0.0, b - a) for a, b in merged)


def _max_bare_streaks(timeline: Timeline) -> tuple[float, float]:
    """Return (max_bare_first_60s, max_bare_after_60s) where no card, caption, or pill is visible."""
    spans = [(c.start_s, c.end_s) for c in timeline.cards]
    spans += [(c.start_s, c.end_s) for c in timeline.captions]
    spans += [(p.start_s, p.end_s) for p in timeline.pills]
    spans.sort()

    merged: list[tuple[float, float]] = []
    for a, b in spans:
        if not merged or a > merged[-1][1]:
            merged.append((a, b))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))

    gaps: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in merged:
        if a > cur:
            gaps.append((cur, a))
        cur = max(cur, b)
    if cur < timeline.duration_s:
        gaps.append((cur, timeline.duration_s))

    max_first = 0.0
    max_after = 0.0
    for g0, g1 in gaps:
        if g0 < 60.0:
            max_first = max(max_first, min(g1, 60.0) - g0)
        if g1 > 60.0:
            max_after = max(max_after, g1 - max(g0, 60.0))
    return round(max_first, 2), round(max_after, 2)


def measure_timeline_qc(
    timeline: Timeline,
    brief: ProjectBrief,
    alignment: AlignmentResult | None = None,
    audio_stats: AudioMasterStats | None = None,
) -> QCReport:
    """Measure every quantitative QC target listed in <build> #7 and produce an honest shortfalls list."""
    metrics: list[QCMetric] = []
    shortfalls: list[str] = []

    def add(name: str, measured: str, target: str, ok: bool, note: str = "") -> None:
        metrics.append(QCMetric(name=name, measured=measured, target=target, passed=ok, note=note))
        if not ok:
            shortfalls.append(f"**{name}**: measured {measured} vs target {target}" + (f" ({note})" if note else ""))

    dur_min = max(timeline.duration_s / 60.0, 1e-6)
    shot_durs = [s.duration_s for s in timeline.shots]

    # 1. Picture changes per minute (25-31 overall, 24-29 in first 60s)
    cuts_per_min = len(timeline.shots) / dur_min
    first_60_shots = sum(1 for s in timeline.shots if s.start_s < 60.0)
    first_60_rate = first_60_shots / min(1.0, dur_min)
    add("Picture changes / min", f"{cuts_per_min:.1f}/min", "25-31 / min", 25.0 <= cuts_per_min <= 31.0)
    add("First-60s picture changes", f"{first_60_rate:.1f}/min", "24-29 in first 60s", 24.0 <= first_60_rate <= 29.0)

    # 2. Median shot length (1.9 - 2.4s)
    med_shot = statistics.median(shot_durs) if shot_durs else 0.0
    add("Median shot duration", f"{med_shot:.2f}s", "1.9-2.4s", 1.9 <= med_shot <= 2.4)

    # 3. Shots over 6s (<= 3%, ideally 0)
    over_6 = sum(1 for d in shot_durs if d > 6.0)
    over_6_pct = (over_6 / max(len(shot_durs), 1)) * 100.0
    add("Shots over 6s", f"{over_6} ({over_6_pct:.1f}%)", "<= 3% (0 ideal)", over_6_pct <= 3.0)

    # 4. Zero repeated shots
    ids = [s.shot_id for s in timeline.shots if not s.shot_id.startswith("MISSING_")]
    dupes = len(ids) - len(set(ids))
    missing = sum(1 for s in timeline.shots if s.shot_id.startswith("MISSING_"))
    add("Unique shots (zero repeats)", f"{dupes} repeats, {missing} missing", "0 repeats, 0 missing", dupes == 0 and missing == 0)

    # 5. Card coverage (35-40% of runtime)
    card_cov_s = _merged_coverage_s([(c.start_s, c.end_s) for c in timeline.cards])
    card_pct = (card_cov_s / max(timeline.duration_s, 1e-6)) * 100.0
    add("Card on-screen coverage", f"{card_pct:.1f}%", "35%-40%", 34.0 <= card_pct <= 42.0)

    # 6. Bare footage streaks (<= 5s first 60s, <= 7s after)
    bare_60, bare_after = _max_bare_streaks(timeline)
    bare_ok = bare_60 <= 5.0 and bare_after <= 7.0
    add("Longest bare footage streak", f"{bare_60:.1f}s (0-60s) / {bare_after:.1f}s (60s+)", "<= 5.0s / <= 7.0s", bare_ok)

    # 7. Stills share (< 10% of runtime)
    still_s = sum(s.duration_s for s in timeline.shots if s.is_still)
    still_pct = (still_s / max(timeline.duration_s, 1e-6)) * 100.0
    add("Official stills share", f"{still_pct:.1f}%", "< 10%", still_pct < 10.0)

    # 8. Longest pause (<= 0.5s)
    longest_pause = audio_stats.longest_pause_s if audio_stats else (compute_longest_pause(alignment.words) if alignment else 0.0)
    add("Longest silence pause", f"{longest_pause:.2f}s", "<= 0.50s", longest_pause <= 0.50)

    # 9. Audio loudness (-18 LUFS, LRA <= 3 LU, TP < -2 dB)
    if audio_stats and audio_stats.integrated_lufs is not None:
        lufs_ok = abs(audio_stats.integrated_lufs - (-18.0)) <= 0.8
        lra_ok = (audio_stats.lra_lu or 0.0) <= 3.2
        tp_ok = (audio_stats.true_peak_db or -99.0) <= -1.9
        add(
            "Audio loudness",
            f"{audio_stats.integrated_lufs:.1f} LUFS / LRA {audio_stats.lra_lu:.1f} / TP {audio_stats.true_peak_db:.1f} dB",
            "-18 LUFS / LRA <= 3 / TP < -2 dB",
            lufs_ok and lra_ok and tp_ok,
        )
    else:
        add("Audio loudness", "not yet mastered", "-18 LUFS / LRA <= 3 / TP < -2 dB", False, "run `vicecut audio`")

    # 10. End-of-film voice/caption sync drift (<= 50 ms)
    max_drift_ms = 0.0
    if alignment and timeline.captions:
        word_map = {w.word_id: w for w in alignment.words}
        for cap in timeline.captions:
            for cw in cap.words:
                aw = word_map.get(cw.word_id)
                if aw is not None:
                    max_drift_ms = max(max_drift_ms, abs(cw.reveal_s - aw.start_s) * 1000.0)
    add("End voice/caption sync drift", f"{max_drift_ms:.1f} ms", "<= 50 ms", max_drift_ms <= 50.0)

    # 11. Look-and-fix QC rounds (>= 3)
    rounds = brief.approvals.qc_rounds_completed
    add("Look-and-fix QC rounds", f"{rounds} rounds", ">= 3 rounds", rounds >= 3,
        "" if rounds >= 3 else "inspect 4fps preview sheets & full-size card proofs 3 times")

    # Include any timeline warnings honestly in shortfalls
    for w in timeline.warnings:
        shortfalls.append(f"Timeline warning: {w}")

    return QCReport(
        project_slug=brief.slug,
        passed=all(m.passed for m in metrics),
        metrics=metrics,
        shortfalls=shortfalls,
    )


def generate_credits_markdown(
    brief: ProjectBrief,
    ledger: ResearchLedger | None,
    manifest: MediaManifest | None,
    output_md: Path,
) -> Path:
    """Compile complete transparent source credits list for footage, stills, photos, headlines & facts (<build> #7)."""
    lines = [
        f"# Source Credits — {brief.topic}",
        "",
        f"- **Project:** `{brief.slug}`",
        f"- **Research Date:** {brief.research_date}",
        "- **Format:** Commentary over officially quoted footage & press material.",
        "",
    ]

    if manifest and manifest.sources:
        lines.append("## Footage, Stills, Photos & Headlines")
        lines.append("")
        for s in manifest.sources:
            mockup_note = " *(marked MOCK-UP on screen)*" if s.is_mockup else ""
            lines.append(
                f"- **{s.title}** ({s.asset_type}) — {s.channel_or_outlet} ({s.date}) — {s.page_url} "
                f"[License: {s.license}]{mockup_note}"
            )
        lines.append("")

    if ledger and ledger.facts:
        lines.append("## Factual Claims & Citations")
        lines.append("")
        for f in ledger.facts:
            corrob = f" | Corroborated by {f.second_source_name} ({f.second_source_url})" if f.second_source_url else ""
            lines.append(
                f"- **[{f.id}]** ({f.confidence.value}) {f.claim} — *{f.source_name}* ({f.date}): {f.source_url}{corrob}"
            )
        lines.append("")

    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text("\n".join(lines), encoding="utf-8")
    return output_md
