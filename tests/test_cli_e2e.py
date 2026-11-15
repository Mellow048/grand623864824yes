"""End-to-end CLI smoke tests exercising the vicecut command interface."""

from __future__ import annotations

import shutil
from pathlib import Path

from vicecut.cli import main as cli_main


def test_cli_intake_and_init(tmp_path: Path, capsys) -> None:
    assert cli_main(["intake"]) == 0
    out = capsys.readouterr().out
    assert "1. **Topic**" in out

    preset = Path(__file__).resolve().parent.parent / "presets" / "gta6.yaml"
    rc = cli_main([
        "init",
        "--projects-dir", str(tmp_path),
        "--slug", "cli_proj",
        "--topic", "everything you need to know about GTA 6 before you play",
        "--viewer", "Console players",
        "--length", "sample",
        "--preset", str(preset),
    ])
    assert rc == 0
    assert (tmp_path / "cli_proj" / "project.yaml").exists()


def test_cli_full_dry_pipeline_on_sample_copy(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parent.parent / "examples" / "gta6-sample"
    dst = tmp_path / "sample_run"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.wav", "*.png", "*.html", "timeline.json", "words*.json", "qc_report.md", "credits.md"))

    assert cli_main(["audit-research", str(dst)]) == 0
    assert cli_main(["check", str(dst)]) == 0
    assert cli_main(["media-budget", str(dst)]) == 0
    assert cli_main(["approve", str(dst), "--gate", "script"]) == 0
    assert cli_main(["voice", str(dst)]) == 0
    assert cli_main(["align", str(dst), "--synthetic"]) == 0
    assert cli_main(["timeline", str(dst)]) == 0
    assert cli_main(["approve", str(dst), "--gate", "storyboard"]) == 0
    assert cli_main(["render", str(dst), "--mode", "preview", "--dry-run"]) == 0
    assert cli_main(["render", str(dst), "--mode", "mobile", "--dry-run"]) == 0

    # QC exits 1 honestly because audio mastering & 3 visual rounds haven't run yet,
    # while still generating qc_report.md and credits.md cleanly.
    assert cli_main(["qc", str(dst)]) == 1
    assert (dst / "renders" / "qc_report.md").exists()
    assert (dst / "renders" / "credits.md").exists()
    assert (dst / "timeline" / "storyboard_sheet.png").exists()
    assert (dst / "renders" / "overlay_page.html").exists()
