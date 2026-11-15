"""Tests for config, palette extraction, file-only key security, intake & project scaffolding."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from vicecut.config import (
    ProjectBrief,
    ThemeConfig,
    VoiceConfig,
    extract_palette_from_key_art,
)
from vicecut.intake import (
    TopicPitch,
    build_intake_message,
    parse_topic_line,
    scaffold_project,
)


def test_gta6_default_theme_tokens() -> None:
    t = ThemeConfig()
    assert t.panel_bg == "#1E1322"
    assert t.border_color == "#F7D3EA"
    assert t.glow_rgb == (255, 120, 205)
    assert t.gradient_start == "#FF93DC"
    assert t.gradient_end == "#FFB27C"
    assert t.pill_pink == "#FB96E6"
    assert t.pill_cream == "#FFF1DE"
    assert t.pill_peach == "#FFBA90"
    assert t.font_display == "Montserrat"
    assert t.blur_px == 26


def test_extract_palette_from_key_art(tmp_path: Path) -> None:
    art = Image.new("RGB", (200, 120), (30, 195, 220))
    for x in range(100, 200):
        for y in range(120):
            art.putpixel((x, y), (70, 130, 240))
    art_path = tmp_path / "key_art.png"
    art.save(art_path)

    theme = extract_palette_from_key_art(art_path)
    assert theme.gradient_start.startswith("#") and len(theme.gradient_start) == 7
    assert theme.gradient_end.startswith("#") and len(theme.gradient_end) == 7
    assert theme.gradient_start != theme.gradient_end
    assert theme.derived_from_key_art == str(art_path)


def test_elevenlabs_api_key_read_only_from_file_and_never_saved_in_yaml(tmp_path: Path) -> None:
    secret_file = tmp_path / "el.key"
    secret_file.write_text("sk_super_secret_do_not_leak_999\n", encoding="utf-8")

    vc = VoiceConfig(provider="elevenlabs", api_key_file=str(secret_file))
    assert vc.load_api_key() == "sk_super_secret_do_not_leak_999"

    brief = ProjectBrief(
        slug="secret-test",
        topic="everything you need to know about GTA 6 before you buy",
        subject="GTA 6",
        action="buy",
        viewer="Gamers",
        voice=vc,
    )
    yaml_path = tmp_path / "project.yaml"
    brief.save(yaml_path)
    raw_yaml = yaml_path.read_text(encoding="utf-8")
    assert "sk_super_secret_do_not_leak_999" not in raw_yaml

    missing = VoiceConfig(provider="elevenlabs", api_key_file=str(tmp_path / "missing.key"))
    with pytest.raises(FileNotFoundError):
        missing.load_api_key()


def test_intake_message_and_topic_parser() -> None:
    msg = build_intake_message()
    assert "1. **Topic**" in msg
    assert "6. **Palette**" in msg
    assert "sample" in msg and "medium" in msg and "full" in msg

    subj, act = parse_topic_line("everything you need to know about GTA 6 before you buy")
    assert subj == "GTA 6"
    assert act == "buy"

    with pytest.raises(ValueError):
        parse_topic_line("random topic without valid template")


def test_topic_pitch_30_day_window() -> None:
    today = date(2026, 11, 15)
    p_ok = TopicPitch(
        topic_line="everything you need to know about GTA 6 before you play",
        event_date="2026-11-19",
        why_now="Console release window",
    )
    p_late = TopicPitch(
        topic_line="everything you need to know about Next Console before you buy",
        event_date="2027-02-01",
        why_now="Too far away",
    )
    assert p_ok.is_within_30_days(today) is True
    assert p_late.is_within_30_days(today) is False


def test_scaffold_project_with_gta6_preset(tmp_path: Path) -> None:
    preset_path = Path(__file__).resolve().parent.parent / "presets" / "gta6.yaml"
    proj_dir, brief = scaffold_project(
        projects_root=tmp_path,
        slug="my_gta6_vid",
        topic="everything you need to know about GTA 6 before you play",
        viewer="PS5 and Xbox Series X players",
        length_preset="sample",
        preset_path=preset_path,
    )
    assert brief.subject == "GTA 6"
    assert brief.action == "play"
    assert (proj_dir / "project.yaml").exists()
    assert (proj_dir / "research" / "facts.yaml").exists()
    assert (proj_dir / "script" / "script.md").exists()
    assert (proj_dir / "script" / "pronunciations.yaml").exists()
    assert (proj_dir / "media" / "manifest.yaml").exists()
    assert "Rockstar Games" in brief.media.allowed_official_channels
