"""Six-question intake prompt, topic parsing, 30-day pitch validator, and project scaffolder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from .config import (
    ApprovalState,
    MediaPreferences,
    ProjectBrief,
    ThemeConfig,
    VoiceConfig,
    extract_palette_from_key_art,
    project_dirs,
)

TOPIC_PATTERN = re.compile(
    r"^\s*everything\s+you\s+need\s+to\s+know\s+about\s+(?P<subject>.+?)\s+before\s+you\s+"
    r"(?P<action>play|buy|watch|visit|file|switch)\b.*$",
    re.IGNORECASE,
)


@dataclass
class TopicPitch:
    """A candidate topic launching or peaking within 30 days."""

    topic_line: str
    event_date: str  # YYYY-MM-DD
    why_now: str

    def is_within_30_days(self, today: date | None = None) -> bool:
        ref = today or date.today()
        target = date.fromisoformat(self.event_date)
        return ref <= target <= ref + timedelta(days=30)


def build_intake_message() -> str:
    """Return the exact six input questions in one message (<inputs> + <start>)."""
    return (
        "Before I research anything or write a single line, give me these 6 inputs:\n"
        "1. **Topic** (one line): \"everything you need to know about ___ before you ___ "
        "(play, buy, watch, visit, file, switch)\" — or say **none** and I'll pitch 3 topics "
        "launching or peaking in the next 30 days with exact dates.\n"
        "2. **Viewer**: who they are and what they are about to do.\n"
        "3. **Length**: `sample` (60–90 s), `medium` (5 minutes), or `full` (20–35 minutes).\n"
        "4. **Voice**: an ElevenLabs voice ID + path to a local key file (I'll show you the exact "
        "character count at ~1 credit each and wait for your yes before spending anything), "
        "or leave blank for the free local Kokoro voice.\n"
        "5. **Footage & source rules**: any footage paths you already have, plus any outlet, channel, "
        "or site you do NOT want used.\n"
        "6. **Palette**: two gradient hex colours (default: Vice City `#FF93DC` → `#FFB27C` on `#1E1322`), "
        "or pass a key-art image path so I pull two neighbouring hues from it.\n\n"
        "_Everything will be researched fresh today. I will show you the sourced script, source list, "
        "and automated checker result for your approval before recording or downloading anything._"
    )


def parse_topic_line(topic: str) -> tuple[str, str]:
    """Parse 'everything you need to know about <subject> before you <action>'."""
    m = TOPIC_PATTERN.match(topic.strip())
    if not m:
        raise ValueError(
            "Topic must match: 'everything you need to know about ___ before you ___ "
            "(play, buy, watch, visit, file, switch)'"
        )
    return m.group("subject").strip(), m.group("action").strip().lower()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "explainer-project"


def load_preset(preset_path: Path) -> dict[str, Any]:
    return yaml.safe_load(preset_path.read_text(encoding="utf-8")) or {}


def scaffold_project(
    projects_root: Path,
    topic: str,
    viewer: str,
    length_preset: str = "sample",
    voice_provider: str = "kokoro",
    voice_id: str = "af_heart",
    api_key_file: str | None = None,
    existing_footage: list[str] | None = None,
    blocked_sources: list[str] | None = None,
    gradient_colors: tuple[str, str] | None = None,
    key_art_path: Path | None = None,
    preset_path: Path | None = None,
    motif: str = "",
    slug: str | None = None,
) -> tuple[Path, ProjectBrief]:
    """Create a complete per-video workspace with starter research, script, and media files."""
    subject, action = parse_topic_line(topic)
    project_slug = slug or slugify(f"{subject}-before-you-{action}")
    proj_root = projects_root / project_slug
    dirs = project_dirs(proj_root)
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    preset_data: dict[str, Any] = load_preset(preset_path) if preset_path and preset_path.exists() else {}

    # Build ThemeConfig from preset -> optional key-art -> optional explicit hex pair
    theme_dict = dict(preset_data.get("theme", {}))
    if "glow_rgb" in theme_dict and isinstance(theme_dict["glow_rgb"], list):
        theme_dict["glow_rgb"] = tuple(int(v) for v in theme_dict["glow_rgb"][:3])
    theme = ThemeConfig(**theme_dict) if theme_dict else ThemeConfig()

    if key_art_path is not None:
        theme = extract_palette_from_key_art(key_art_path, name=f"{project_slug}-key-art")
    elif gradient_colors is not None:
        theme.gradient_start, theme.gradient_end = gradient_colors

    media_preset = preset_data.get("media", {})
    allowed_channels = list(media_preset.get("allowed_official_channels", []))
    merged_blocked = list(dict.fromkeys(list(media_preset.get("blocked_sources", [])) + list(blocked_sources or [])))

    brief = ProjectBrief(
        slug=project_slug,
        topic=topic.strip(),
        subject=subject,
        action=action,
        viewer=viewer.strip(),
        length_preset=length_preset,
        motif=motif or preset_data.get("default_motif", "wait for real proof before you spend"),
        voice=VoiceConfig(
            provider=voice_provider,
            voice_id=voice_id,
            api_key_file=api_key_file,
        ),
        media=MediaPreferences(
            existing_footage=list(existing_footage or []),
            allowed_official_channels=allowed_channels,
            blocked_sources=merged_blocked,
        ),
        theme=theme,
        approvals=ApprovalState(),
    )
    brief.save(proj_root / "project.yaml")

    # Seed empty research ledger
    facts_file = dirs["research"] / "facts.yaml"
    if not facts_file.exists():
        facts_file.write_text(
            yaml.safe_dump(
                {
                    "topic": brief.topic,
                    "research_date": brief.research_date,
                    "required_facts": brief.required_fact_count(),
                    "notes": "Research fresh on the day. Open every page. Confirm with a second source.",
                    "facts": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    # Seed pronunciation lexicon from preset
    pron_file = dirs["script"] / "pronunciations.yaml"
    if not pron_file.exists():
        pron_map = dict(preset_data.get("pronunciations", {}))
        pron_file.write_text(
            yaml.safe_dump({"overrides": pron_map}, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    # Seed empty media manifest
    manifest_file = dirs["media"] / "manifest.yaml"
    if not manifest_file.exists():
        manifest_file.write_text(
            yaml.safe_dump(
                {
                    "project": project_slug,
                    "sources": [],
                    "shots": [],
                    "headlines": [],
                    "photos": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    # Seed declarative media/fetch_plan.yaml for 1-click GitHub Actions / local `vicecut media-fetch`
    fetch_plan_file = dirs["media"] / "fetch_plan.yaml"
    if not fetch_plan_file.exists():
        fetch_plan_file.write_text(
            f"# Declarative official media fetch plan for `vicecut media-fetch` and GitHub Actions.\n"
            f"# Populate ONLY with user-approved official publisher URLs (never fan-made/concept/leaks).\n"
            f"project: {brief.slug}\n"
            f"videos: []\n"
            f"# Example video entry:\n"
            f"#   - id: src_trailer1\n"
            f"#     title: \"Official Trailer 1\"\n"
            f"#     channel_or_outlet: \"Rockstar Games\"\n"
            f"#     url: \"https://www.youtube.com/watch?v=...\"\n"
            f"#     page_url: \"https://www.rockstargames.com/VI\"\n"
            f"#     date: \"{brief.research_date}\"\n"
            f"#     scene_threshold: 0.28\n"
            f"#     drop_first_s: 2.5\n"
            f"#     drop_last_s: 4.0\n"
            f"photos: []\n"
            f"headlines: []\n",
            encoding="utf-8",
        )

    # Seed starter annotated script template
    script_file = dirs["script"] / "script.md"
    if not script_file.exists():
        script_file.write_text(
            f"# {brief.topic}\n"
            f"<!-- One sentence per line. Mark [FACT:F01], [SOURCE:Name], [TEASER:T1], [PAYOFF:T1], "
            f"[END_PROMISE], [END_TEASE], [FLAG], [SO], [JOKE], [ASK], [MOTIF], [HANDOFF], [BRIDGE] -->\n\n"
            f"## Intro\n\n"
            f"## Section 1: First Topic\n\n"
            f"## Close\n",
            encoding="utf-8",
        )

    return proj_root, brief
