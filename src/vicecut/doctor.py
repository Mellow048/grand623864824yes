"""Local free toolchain diagnostics and guided installer (<build> #1)."""

from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolCheck:
    name: str
    required: bool
    installed: bool
    detail: str
    install_hint: str


def _check_python_module(mod_name: str) -> tuple[bool, str]:
    spec = importlib.util.find_spec(mod_name)
    if spec is None:
        return False, "not installed"
    try:
        mod = __import__(mod_name)
        ver = getattr(mod, "__version__", "installed")
        return True, str(ver)
    except Exception as exc:  # pragma: no cover
        return False, f"import error: {exc}"


def _check_binary(binary: str, version_flag: str = "-version") -> tuple[bool, str]:
    path = shutil.which(binary)
    if not path:
        return False, "not found in PATH"
    try:
        proc = subprocess.run(
            [path, version_flag],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        first_line = (proc.stdout or proc.stderr or "found").strip().splitlines()[0]
        return True, first_line[:90]
    except Exception as exc:  # pragma: no cover
        return True, f"found at {path} ({exc})"


def _check_montserrat_fonts() -> tuple[bool, str]:
    """Check whether Montserrat Black (900) and Bold (700) are installed locally."""
    search_dirs = [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local/share/fonts",
        Path.home() / "Library/Fonts",
        Path("/Library/Fonts"),
        Path("C:/Windows/Fonts"),
    ]
    found: list[str] = []
    for base in search_dirs:
        if not base.exists():
            continue
        for p in base.rglob("*Montserrat*"):
            if p.suffix.lower() in {".ttf", ".otf", ".woff2"}:
                found.append(p.name)
    has_black = any("black" in f.lower() or "variable" in f.lower() or "wght" in f.lower() for f in found)
    has_bold = any("bold" in f.lower() or "variable" in f.lower() or "wght" in f.lower() for f in found)
    if has_black and has_bold:
        return True, f"found ({', '.join(sorted(set(found))[:4])})"
    if found:
        return False, f"partial ({', '.join(sorted(set(found))[:4])}); need Black & Bold"
    return False, "not found in system font directories"


def run_doctor(elevenlabs_key_file: str | None = None) -> list[ToolCheck]:
    """Run full environment diagnostics for every tool specified in <build> #1."""
    py_ok = sys.version_info >= (3, 10)
    checks: list[ToolCheck] = [
        ToolCheck(
            name="Python 3.10+",
            required=True,
            installed=py_ok,
            detail=platform.python_version(),
            install_hint="Install Python 3.10+ from https://www.python.org/",
        )
    ]

    for label, mod, hint in [
        ("numpy", "numpy", "pip install numpy"),
        ("Pillow", "PIL", "pip install Pillow"),
        ("PyYAML", "yaml", "pip install PyYAML"),
        ("playwright (Python)", "playwright", "pip install playwright && playwright install chromium"),
        ("faster-whisper (large-v3-turbo int8)", "faster_whisper", "pip install faster-whisper"),
        ("kokoro-onnx (free local voice)", "kokoro_onnx", "pip install kokoro-onnx"),
    ]:
        ok, detail = _check_python_module(mod)
        checks.append(ToolCheck(label, required=(mod != "kokoro_onnx" or not elevenlabs_key_file), installed=ok, detail=detail, install_hint=hint))

    for label, binary, flag, hint in [
        ("FFmpeg", "ffmpeg", "-version", "apt install ffmpeg / brew install ffmpeg"),
        ("ffprobe", "ffprobe", "-version", "installed with FFmpeg"),
        ("yt-dlp", "yt-dlp", "--version", "pip install yt-dlp"),
    ]:
        ok, detail = _check_binary(binary, flag)
        checks.append(ToolCheck(label, required=True, installed=ok, detail=detail, install_hint=hint))

    font_ok, font_detail = _check_montserrat_fonts()
    checks.append(
        ToolCheck(
            name="Montserrat Black & Bold (SIL OFL)",
            required=True,
            installed=font_ok,
            detail=font_detail,
            install_hint="Download free SIL OFL Montserrat Black & Bold from Google Fonts into ~/.local/share/fonts/",
        )
    )

    if elevenlabs_key_file:
        p = Path(elevenlabs_key_file).expanduser()
        exists = p.exists() and p.is_file() and p.stat().st_size > 0
        checks.append(
            ToolCheck(
                name="ElevenLabs key file (never printed)",
                required=True,
                installed=exists,
                detail="present & non-empty (value hidden)" if exists else f"missing at {p}",
                install_hint=f"Place your ElevenLabs key in {p} with chmod 600",
            )
        )

    return checks


def format_doctor_report(checks: list[ToolCheck]) -> str:
    """Format diagnostic checks into a clear terminal report."""
    lines = ["ViceCut Environment Diagnostics", "=" * 64]
    missing: list[ToolCheck] = []
    for c in checks:
        status = "OK  " if c.installed else ("MISS" if c.required else "OPT ")
        lines.append(f"[{status}] {c.name:<38} {c.detail}")
        if not c.installed:
            missing.append(c)
    if missing:
        lines.append("")
        lines.append("Install commands for missing tools:")
        for c in missing:
            lines.append(f"  - {c.name}: {c.install_hint}")
    else:
        lines.append("")
        lines.append("All required free local tools are installed and ready.")
    return "\n".join(lines)


def install_missing_python_tools(checks: list[ToolCheck], dry_run: bool = False) -> list[str]:
    """Install missing pip-installable packages and report exactly what was installed."""
    pkg_map = {
        "numpy": "numpy",
        "Pillow": "Pillow",
        "PyYAML": "PyYAML",
        "playwright (Python)": "playwright",
        "faster-whisper (large-v3-turbo int8)": "faster-whisper",
        "kokoro-onnx (free local voice)": "kokoro-onnx",
        "yt-dlp": "yt-dlp",
    }
    to_install = [pkg_map[c.name] for c in checks if not c.installed and c.name in pkg_map]
    if not to_install:
        return []
    cmd = [sys.executable, "-m", "pip", "install", *to_install]
    if dry_run:
        return [f"DRY RUN: {' '.join(cmd)}"]
    subprocess.run(cmd, check=True)
    installed = list(to_install)
    if "playwright" in to_install:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        installed.append("playwright-chromium")
    return installed
