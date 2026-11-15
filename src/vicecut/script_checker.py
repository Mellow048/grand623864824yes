"""Automated script checker enforcing every rule in <direction>, <structure>, and <build> #3."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import ProjectBrief, TARGET_WPM_MID
from .research import ConfidenceLevel, REQUIRED_SPOKEN_CAVEATS, ResearchLedger
from .script_parser import ParsedScript, ScriptLine

# Banned phrases from <direction>
BANNED_PHRASES: list[tuple[str, re.Pattern[str]]] = [
    ("greeting", re.compile(r"\b(hey\s+guys|hey\s+everyone|hello\s+everyone|what'?s\s+up\s+guys|welcome\s+back|welcome\s+to\s+(?:the|our|my))\b", re.I)),
    ("narrator/channel self-name", re.compile(r"\b(my\s+name\s+is|on\s+this\s+channel|this\s+channel\s+is|i'?m\s+your\s+host)\b", re.I)),
    ("'in today's video'", re.compile(r"\bin\s+today'?s\s+video\b", re.I)),
    ("'let's dive in'", re.compile(r"\blet'?s\s+(dive|jump|get)\s+(right\s+)?in\b", re.I)),
    ("recap of what was just said", re.compile(r"\b(to\s+recap|as\s+we\s+just\s+saw|so\s+far\s+we'?ve\s+(?:covered|talked\s+about)|let'?s\s+recap)\b", re.I)),
    ("'thanks for watching'", re.compile(r"\b(thanks\s+for\s+watching|thank\s+you\s+for\s+watching|see\s+you\s+in\s+the\s+next\s+(?:one|video))\b", re.I)),
    ("leak used as evidence", re.compile(r"\b(a\s+leak\s+(?:proves|confirms|shows)|leaked\s+footage\s+(?:proves|confirms))\b", re.I)),
]

# Common one-syllable exceptions where naive vowel-group counting overcounts
_ONE_SYLLABLE_WORDS = {
    "are", "base", "came", "care", "case", "change", "check", "choice", "close", "code",
    "come", "console", "course", "crime", "cut", "date", "day", "deal", "done", "drive",
    "each", "else", "end", "face", "fact", "fake", "false", "fine", "fire", "first",
    "five", "force", "frame", "frames", "free", "from", "full", "game", "games", "gave",
    "give", "gone", "good", "gta", "guide", "have", "here", "here's", "high", "home",
    "hope", "hour", "hours", "house", "huge", "it's", "i'm", "i've", "i'll", "keep",
    "know", "large", "last", "late", "launch", "leak", "leaks", "least", "leave", "less",
    "life", "like", "line", "live", "load", "long", "look", "lose", "made", "make",
    "map", "maps", "mean", "means", "might", "mile", "miles", "mode", "modes", "more",
    "most", "move", "much", "must", "name", "near", "need", "needs", "news", "next",
    "nice", "nine", "none", "note", "now", "once", "one", "ones", "own", "pace",
    "page", "paid", "part", "parts", "pay", "pc", "peak", "pick", "piece", "place",
    "plan", "play", "plays", "plus", "point", "price", "proof", "prove", "ps5", "pure",
    "push", "put", "quite", "quote", "race", "rage", "range", "rate", "raw", "read",
    "real", "rest", "right", "role", "room", "rule", "rules", "run", "runs", "safe",
    "said", "same", "save", "say", "says", "scale", "scene", "scenes", "scope", "score",
    "screen", "see", "seen", "sell", "sense", "set", "shape", "share", "she", "shift",
    "short", "shot", "shots", "should", "show", "shown", "shows", "side", "sign", "since",
    "site", "six", "size", "skip", "small", "so", "some", "soon", "source", "space",
    "speak", "spec", "specs", "speed", "spend", "spent", "split", "spot", "stage", "stakes",
    "start", "state", "stay", "step", "steps", "still", "stop", "store", "street", "streets",
    "strike", "strong", "stuff", "style", "such", "sure", "switch", "take", "takes", "talk",
    "team", "tell", "ten", "test", "text", "than", "that", "that's", "the", "their",
    "them", "then", "there", "there's", "these", "they", "they're", "thing", "things", "think",
    "third", "this", "those", "though", "three", "through", "time", "times", "told", "tone",
    "too", "took", "tool", "tools", "top", "touch", "town", "track", "trade", "trait",
    "trap", "treat", "tried", "true", "trust", "truth", "try", "turn", "turns", "twelve",
    "twice", "two", "type", "use", "used", "uses", "vast", "vice", "view", "voice",
    "wait", "want", "wants", "warm", "watch", "way", "ways", "we", "we're", "week",
    "weeks", "weight", "well", "went", "were", "what", "what's", "when", "where", "which",
    "while", "white", "who", "whole", "whose", "why", "wide", "will", "win", "wipe",
    "wise", "with", "won't", "word", "words", "work", "works", "world", "worse", "worst",
    "worth", "would", "wrap", "write", "wrong", "year", "years", "yes", "yet", "you",
    "you'd", "you'll", "you're", "you've", "your", "yours", "zone", "zones",
}


def count_syllables(word: str) -> int:
    """Fast deterministic English syllable estimator tuned for plain spoken prose."""
    w = re.sub(r"[^a-z']", "", word.lower())
    if not w:
        return 1
    if w in _ONE_SYLLABLE_WORDS or len(w) <= 3:
        return 1
    # Strip trailing silent 'e' (unless -le after consonant like 'table')
    core = w
    if core.endswith("es") and len(core) > 4 and core[-3] not in "sxzhcg":
        core = core[:-2]
    elif core.endswith("ed") and len(core) > 4 and core[-3] not in "td":
        core = core[:-2]
    elif core.endswith("e") and not (core.endswith("le") and len(core) > 2 and core[-3] not in "aeiouy"):
        core = core[:-1]
    groups = re.findall(r"[aeiouy]+", core)
    return max(1, len(groups))


@dataclass
class CheckItem:
    rule: str
    status: str  # "PASS" | "WARN" | "FAIL"
    measured: str
    target: str
    detail: str = ""


@dataclass
class ScriptCheckReport:
    passed: bool
    estimated_duration_s: float
    total_words: int
    assumed_wpm: float
    checks: list[CheckItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "estimated_duration_s": round(self.estimated_duration_s, 1),
            "total_words": self.total_words,
            "assumed_wpm": self.assumed_wpm,
            "checks": [asdict(c) for c in self.checks],
            "errors": self.errors,
            "warnings": self.warnings,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def format_text(self) -> str:
        header = f"ViceCut Script Checker — {'PASS' if self.passed else 'FAIL'}"
        lines = [
            header,
            "=" * 78,
            f"Words: {self.total_words} | Est. runtime @ {self.assumed_wpm:.0f} wpm: {self.estimated_duration_s:.1f}s",
            "-" * 78,
        ]
        for c in self.checks:
            lines.append(f"[{c.status:<4}] {c.rule:<32} {c.measured:<18} (target: {c.target})")
            if c.detail:
                lines.append(f"       -> {c.detail}")
        if self.errors:
            lines.append("-" * 78)
            lines.append(f"Blocking issues ({len(self.errors)}):")
            for err in self.errors:
                lines.append(f"  * {err}")
        return "\n".join(lines)


def check_script(
    script: ParsedScript,
    brief: ProjectBrief,
    ledger: ResearchLedger | None = None,
    assumed_wpm: float = TARGET_WPM_MID,
) -> ScriptCheckReport:
    """Run every structural, stylistic, sourcing, and ban rule against a parsed script."""
    checks: list[CheckItem] = []
    errors: list[str] = []
    warnings: list[str] = []

    lines = script.all_lines
    total_words = script.total_words
    if total_words == 0 or not lines:
        return ScriptCheckReport(
            passed=False,
            estimated_duration_s=0.0,
            total_words=0,
            assumed_wpm=assumed_wpm,
            checks=[CheckItem("Non-empty script", "FAIL", "0 words", "> 0 words")],
            errors=["Script contains no spoken sentences."],
        )

    est_duration_s = (total_words / assumed_wpm) * 60.0
    est_minutes = max(est_duration_s / 60.0, 1e-6)
    preset = brief.preset_spec

    def add(rule: str, ok: bool, measured: str, target: str, detail: str = "", warn_only: bool = False) -> None:
        status = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        checks.append(CheckItem(rule=rule, status=status, measured=measured, target=target, detail=detail))
        if not ok:
            msg = f"{rule}: got {measured} (target {target})" + (f" — {detail}" if detail else "")
            if warn_only:
                warnings.append(msg)
            else:
                errors.append(msg)

    # Precompute cumulative word offset -> estimated second for each line
    line_start_s: list[float] = []
    line_end_s: list[float] = []
    cum = 0
    for ln in lines:
        line_start_s.append((cum / assumed_wpm) * 60.0)
        cum += ln.word_count
        line_end_s.append((cum / assumed_wpm) * 60.0)

    # 1. Word count & runtime preset budget
    in_word_budget = preset["min_words"] <= total_words <= preset["max_words"]
    add(
        "Length & word budget",
        in_word_budget,
        f"{total_words} w ({est_duration_s:.0f}s)",
        f"{preset['min_words']}-{preset['max_words']} w",
    )

    # 2. Banned phrases (hard fail)
    banned_hits: list[str] = []
    for ln in lines:
        for label, pat in BANNED_PHRASES:
            if pat.search(ln.text):
                banned_hits.append(f"L{ln.line_no} ({label}): '{ln.text[:60]}'")
    add(
        "Banned phrases",
        len(banned_hits) == 0,
        f"{len(banned_hits)} hits",
        "0 hits",
        "; ".join(banned_hits[:3]),
    )

    # 3. Line 1 rule: fact or 'you' viewer habit
    first_line = lines[0]
    first_ok = first_line.has_tag("FACT") or bool(re.search(r"\b(you|your|you're|you've|you'll)\b", first_line.text, re.I))
    add(
        "Line 1 hook (fact or 'you')",
        first_ok,
        "fact/'you'" if first_ok else "neither",
        "fact or 'you' line",
        f"L{first_line.line_no}: {first_line.text[:65]}" if not first_ok else "",
    )

    # 4. First concrete fact inside 10 seconds
    first_fact_idx = next((i for i, ln in enumerate(lines) if ln.has_tag("FACT")), None)
    if first_fact_idx is not None:
        ff_time = line_start_s[first_fact_idx]
        add("First concrete fact timing", ff_time <= 10.0, f"{ff_time:.1f}s", "<= 10.0s")
    else:
        add("First concrete fact timing", False, "none", "<= 10.0s", "Tag concrete facts with [FACT:F01]")

    # 5. Intro duration (30-65s, 110-230 words) & intro ingredients
    intro = script.intro_section
    if intro is not None:
        intro_s = (intro.word_count / assumed_wpm) * 60.0
        # For 60-90s samples, allow a compact intro if total runtime is short
        if brief.length_preset == "sample":
            intro_ok = 12.0 <= intro_s <= 65.0
            intro_target = "12-65s (sample)"
        else:
            intro_ok = (30.0 <= intro_s <= 65.0) and (110 <= intro.word_count <= 230)
            intro_target = "30-65s (110-230w)"
        add("Intro duration", intro_ok, f"{intro_s:.1f}s ({intro.word_count}w)", intro_target)

        intro_text = intro.spoken_text.lower()
        has_scope = ("everything" in intro_text) or bool(re.search(r"\b(three|four|five|six|seven|eight|nine|ten|\d+)\b", intro_text))
        has_trust = any(p in intro_text for p in ("i checked", "every single", "checked every", "verified every"))
        has_end_promise = any(ln.has_tag("END_PROMISE") for ln in intro.lines) or ("at the very end" in intro_text)
        teasers_in_intro = [v for ln in intro.lines for v in ln.tag_values("TEASER")]

        if brief.length_preset != "sample":
            add("Intro teasers (3-6)", 3 <= len(teasers_in_intro) <= 6, f"{len(teasers_in_intro)}", "3-6 teasers")
            add("Intro scope + trust + end promise", has_scope and has_trust and has_end_promise,
                f"scope={has_scope}, trust={has_trust}, end={has_end_promise}", "all true")
        else:
            add("Intro teasers", len(teasers_in_intro) >= 1, f"{len(teasers_in_intro)}", ">= 1 (sample)")
            add("Intro scope + trust + end promise", has_scope and has_trust,
                f"scope={has_scope}, trust={has_trust}", "scope & trust", warn_only=not has_end_promise)

        # Handoff < 8 words naming section 1
        handoffs = [ln for ln in intro.lines if ln.has_tag("HANDOFF")]
        if handoffs:
            hw = handoffs[-1].word_count
            add("Intro handoff length", hw < 8, f"{hw} words", "< 8 words")
        elif script.body_sections:
            add("Intro handoff length", False, "missing [HANDOFF]", "< 8 words", warn_only=(brief.length_preset == "sample"))

    # 6. Teasers <-> Payoffs integrity (every teaser paid off, never 2 in one section, last in final 10%)
    all_teasers: dict[str, int] = {}
    all_payoffs: dict[str, list[tuple[int, int]]] = {}  # id -> [(section_idx, line_idx)]
    payoffs_per_section: dict[int, list[str]] = {}
    for idx, ln in enumerate(lines):
        for t in ln.tag_values("TEASER"):
            all_teasers[t] = idx
        for p in ln.tag_values("PAYOFF"):
            all_payoffs.setdefault(p, []).append((ln.section_index, idx))
            payoffs_per_section.setdefault(ln.section_index, []).append(p)

    unpaid = [t for t in all_teasers if t not in all_payoffs]
    add("Every teaser paid off", len(unpaid) == 0, f"{len(all_teasers) - len(unpaid)}/{len(all_teasers)}", "100%",
        f"Unpaid: {', '.join(unpaid)}" if unpaid else "")

    multi_payoff_secs = [s for s, ps in payoffs_per_section.items() if len(ps) > 1]
    add("Max 1 payoff per section", len(multi_payoff_secs) == 0,
        f"{len(multi_payoff_secs)} violations", "0 sections with >1 payoff")

    if all_payoffs and brief.length_preset != "sample":
        last_payoff_line = max(idx for pairs in all_payoffs.values() for _, idx in pairs)
        last_pct = line_start_s[last_payoff_line] / max(est_duration_s, 1.0)
        add("Last payoff in final 10%", last_pct >= 0.90, f"at {last_pct*100:.1f}%", ">= 90%")

        # End promise re-teased between 20% and 40%
        end_teases = [i for i, ln in enumerate(lines) if ln.has_tag("END_TEASE")]
        if end_teases:
            et_pct = line_start_s[end_teases[0]] / max(est_duration_s, 1.0)
            add("End promise re-tease window", 0.20 <= et_pct <= 0.40, f"at {et_pct*100:.1f}%", "20%-40%")
        else:
            add("End promise re-tease window", False, "missing [END_TEASE]", "20%-40%")

    # 7. Section counts & duration bounds (0:55 - 4:15 for body sections)
    body_secs = script.body_sections
    if preset["require_full_sections"]:
        add(
            "Body section count",
            preset["body_sections_min"] <= len(body_secs) <= preset["body_sections_max"],
            f"{len(body_secs)} sections",
            f"{preset['body_sections_min']}-{preset['body_sections_max']}",
        )
        bad_secs = []
        for sec in body_secs:
            sec_s = (sec.word_count / assumed_wpm) * 60.0
            if sec_s < 55.0 or sec_s > 255.0:
                bad_secs.append(f"{sec.title} ({sec_s:.0f}s)")
        add("Body section durations", len(bad_secs) == 0, f"{len(bad_secs)} out of bounds", "0:55-4:15 (ideal 2:10-2:40)",
            "; ".join(bad_secs))

    # 8. Subscribe Ask (1 ask between 30%-50%, 2-4 sentences, joined by 'because')
    ask_lines = [(i, ln) for i, ln in enumerate(lines) if ln.has_tag("ASK")]
    if preset["require_subscribe_ask"]:
        if ask_lines:
            ask_start_pct = line_start_s[ask_lines[0][0]] / max(est_duration_s, 1.0)
            ask_count = len(ask_lines)
            ask_text = " ".join(ln.text.lower() for _, ln in ask_lines)
            has_because = "because" in ask_text
            ok_ask = (0.30 <= ask_start_pct <= 0.50) and (2 <= ask_count <= 4) and has_because
            add(
                "Subscribe ask placement & form",
                ok_ask,
                f"{ask_count} sents @ {ask_start_pct*100:.1f}%, because={has_because}",
                "2-4 sents @ 30%-50% with 'because'",
            )
        else:
            add("Subscribe ask placement & form", False, "missing [ASK]", "1 ask @ 30%-50%")

    # 9. Close structure (< 60s, plan + hot take + either/or + motif signoff)
    close = script.close_section
    if close is not None and close is not intro:
        close_s = (close.word_count / assumed_wpm) * 60.0
        add("Close duration", close_s <= 60.0, f"{close_s:.1f}s", "<= 60.0s")
        close_low = close.spoken_text.lower()
        has_plan = any(ln.has_tag("PLAN") for ln in close.lines)
        has_hot = any(ln.has_tag("HOT_TAKE") for ln in close.lines) or ("hot take" in close_low)
        has_choice = any(ln.has_tag("EITHER_OR") for ln in close.lines) or ("you tell me" in close_low)
        add(
            "Close elements (plan/hot take/either-or)",
            has_plan and has_hot and has_choice,
            f"plan={has_plan}, hot={has_hot}, choice={has_choice}",
            "all 3 present",
            warn_only=(brief.length_preset == "sample"),
        )

    # 10. Sentence length metrics: mean 12-14, >=28% <=8 words, max <=50
    lengths = [ln.word_count for ln in lines]
    mean_len = sum(lengths) / len(lengths)
    short_ratio = sum(1 for n in lengths if n <= 8) / len(lengths)
    max_len = max(lengths)
    add("Mean sentence length", 11.5 <= mean_len <= 14.5, f"{mean_len:.1f} words", "12-14 words")
    add("Short sentences (<= 8 words)", short_ratio >= 0.28, f"{short_ratio*100:.1f}%", ">= 28%")
    add("Max sentence length", max_len <= 50, f"{max_len} words", "<= 50 words")

    # 11. One-syllable word ratio >= 72%
    all_words = [w for ln in lines for w in ln.words]
    one_syl = sum(1 for w in all_words if count_syllables(w) == 1)
    one_syl_ratio = one_syl / max(len(all_words), 1)
    add("One-syllable words", one_syl_ratio >= 0.72, f"{one_syl_ratio*100:.1f}%", ">= 72%")

    # 12. Pronoun voice ratios: you/your >= 8/min, I 2-4/min, we near zero
    low_words = [w.lower().replace("’", "'") for w in all_words]
    you_count = sum(1 for w in low_words if w in {"you", "your", "yours", "you're", "you've", "you'll", "you'd", "yourself"})
    i_count = sum(1 for w in low_words if w in {"i", "i'm", "i've", "i'll", "i'd", "my", "me"})
    we_count = sum(1 for w in low_words if w in {"we", "we're", "we've", "we'll", "our", "ours", "us"})

    you_pm = you_count / est_minutes
    i_pm = i_count / est_minutes
    we_pm = we_count / est_minutes
    add("'You/your' cadence", you_pm >= 8.0, f"{you_pm:.1f}/min", ">= 8.0/min")
    add("'I' cadence", 1.8 <= i_pm <= 4.5, f"{i_pm:.1f}/min", "2-4/min", warn_only=True)
    add("'We' avoidance", we_pm <= 1.0, f"{we_pm:.1f}/min ({we_count})", "almost never (<=1/min)")

    # 13. Sentences starting with 'And' or 'So' (30-40%)
    and_so_starts = sum(1 for ln in lines if ln.words and ln.words[0].lower() in {"and", "so"})
    and_so_ratio = and_so_starts / len(lines)
    add("Sentences starting 'And'/'So'", 0.28 <= and_so_ratio <= 0.44, f"{and_so_ratio*100:.1f}%", "30%-40%")

    # 14. Named source cadence (avg <= 20s, max gap <= 85s) & every fact has a named source nearby
    source_times = [line_start_s[i] for i, ln in enumerate(lines) if ln.has_tag("SOURCE")]
    if source_times:
        avg_source_gap = est_duration_s / len(source_times)
        gaps = [source_times[0]] + [b - a for a, b in zip(source_times, source_times[1:])] + [est_duration_s - source_times[-1]]
        max_source_gap = max(gaps)
        add("Named source frequency", avg_source_gap <= 22.0, f"every {avg_source_gap:.1f}s", "<= 20s avg")
        add("Max gap without named source", max_source_gap <= 85.0, f"{max_source_gap:.1f}s", "<= 85s max")
    else:
        add("Named source frequency", False, "0 [SOURCE] tags", "every <= 20s")

    # 15. 'So' actionable line cadence (at least every 30s)
    so_times = [
        line_start_s[i]
        for i, ln in enumerate(lines)
        if ln.has_tag("SO") or (ln.words and ln.words[0].lower() == "so")
    ]
    if so_times:
        so_gaps = [so_times[0]] + [b - a for a, b in zip(so_times, so_times[1:])] + [est_duration_s - so_times[-1]]
        max_so_gap = max(so_gaps)
        avg_so_gap = est_duration_s / len(so_times)
        add("'So' action-line cadence", avg_so_gap <= 30.0 and max_so_gap <= 45.0,
            f"avg {avg_so_gap:.1f}s (max {max_so_gap:.1f}s)", "<= 30s")
    else:
        add("'So' action-line cadence", False, "0 'So' lines", "at least every 30s")

    # 16. Fact unit rules: claim <= 15 words, never 2 jokes in a row, non-official confidence caveat
    long_claims = [f"L{ln.line_no} ({ln.word_count}w)" for ln in lines if ln.has_tag("FACT") and ln.word_count > 16]
    add("Fact claim length (<= 15w)", len(long_claims) == 0, f"{len(long_claims)} over", "<= 15 words",
        ", ".join(long_claims[:4]), warn_only=True)

    consec_jokes = []
    for a, b in zip(lines, lines[1:]):
        if a.has_tag("JOKE") and b.has_tag("JOKE"):
            consec_jokes.append(f"L{a.line_no}-L{b.line_no}")
    add("No consecutive jokes", len(consec_jokes) == 0, f"{len(consec_jokes)} pairs", "0 consecutive jokes")

    # Verify facts against ledger if provided
    if ledger is not None:
        missing_in_ledger = []
        missing_caveats = []
        for idx, ln in enumerate(lines):
            for fid in ln.tag_values("FACT"):
                fact = ledger.fact_by_id(fid)
                if fact is None:
                    missing_in_ledger.append(f"L{ln.line_no}:{fid}")
                elif fact.confidence in REQUIRED_SPOKEN_CAVEATS:
                    window_text = " ".join(l.text.lower() for l in lines[idx : min(len(lines), idx + 3)])
                    cues = REQUIRED_SPOKEN_CAVEATS[fact.confidence]
                    if not any(c in window_text for c in cues):
                        missing_caveats.append(f"L{ln.line_no}:{fid} ({fact.confidence.value})")
        add("All [FACT] IDs in research ledger", len(missing_in_ledger) == 0,
            f"{len(missing_in_ledger)} missing", "0 missing", ", ".join(missing_in_ledger[:4]))
        add("Non-official confidence caveats", len(missing_caveats) == 0,
            f"{len(missing_caveats)} missing", "0 missing", ", ".join(missing_caveats[:4]))

    # 17. Advice motif repetition (>= 4 times for medium/full, >= 2 for sample, AND in final line)
    motif_lines = [i for i, ln in enumerate(lines) if ln.has_tag("MOTIF")]
    if brief.motif:
        motif_key = brief.motif.lower().strip()
        for i, ln in enumerate(lines):
            if motif_key in ln.text.lower() and i not in motif_lines:
                motif_lines.append(i)
    min_motif = 4 if brief.length_preset != "sample" else 2
    in_last = (len(lines) - 1) in motif_lines
    add(
        "Advice motif repetition",
        len(motif_lines) >= min_motif and in_last,
        f"{len(motif_lines)}x (last={in_last})",
        f">= {min_motif}x including final line",
    )

    passed = len(errors) == 0
    return ScriptCheckReport(
        passed=passed,
        estimated_duration_s=est_duration_s,
        total_words=total_words,
        assumed_wpm=assumed_wpm,
        checks=checks,
        errors=errors,
        warnings=warnings,
    )
