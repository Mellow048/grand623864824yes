"""Project configuration, theme tokens, length presets, approval gates, and key-art palette extraction."""

from __future__ import annotations

import colorsys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image


# ---------------------------------------------------------------------------
# Spec-Defined Production Constants
# ---------------------------------------------------------------------------
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
VIDEO_FPS = 30

TARGET_WPM_MIN = 205.0
TARGET_WPM_MAX = 215.0
TARGET_WPM_MID = 210.0

PREVIEW_WIDTH = 960
PREVIEW_HEIGHT = 540
MOBILE_MAX_MB = 25.0

AUDIO_TARGET_LUFS = -18.0
AUDIO_MAX_LRA = 3.0
AUDIO_MAX_TRUE_PEAK_DB = -2.0
SILENCE_THRESHOLD_DB = -35.0
SILENCE_MAX_KEEP_S = 0.30
SILENCE_PAD_S = 0.035
MAX_TEMPO_SPEEDUP = 1.12

BLUR_RADIUS_PX = 26
CARD_LEAD_IN_S = 0.12
CARD_FADE_S = 0.25
CARD_MIN_HOLD_S = 1.2

CARD_HOLDS_S: dict[str, float] = {
    "photo": 2.6,
    "headline": 2.8,
    "character": 2.8,
    "prop": 2.9,       # Animated white-background studio object prop (zero people!)
    "number": 1.5,
    "chapter": 2.0,
    # Quote holds until 0.9s after its last spoken word (computed dynamically).
    "quote_tail": 0.9,
}

CARD_DARKEN_ALPHA: dict[str, float] = {
    "photo": 0.42,
    "headline": 0.50,
    "character": 0.0,   # Sharp footage, no darkening
    "prop": 0.0,        # Compact floating PiP card — keep full-motion gameplay crisp & unblurred behind it!
    "quote": 0.55,
    "number": 0.48,
    "chapter": 0.60,
}

LENGTH_PRESETS: dict[str, dict[str, Any]] = {
    "sample": {
        "label": "60-90s sample",
        "min_duration_s": 60,
        "max_duration_s": 90,
        "target_duration_s": 75,
        "min_words": 205,
        "max_words": 325,
        "target_words": 265,
        "body_sections_min": 0,
        "body_sections_max": 2,
        "require_subscribe_ask": False,
        "require_full_sections": False,
    },
    "medium": {
        "label": "5-minute explainer",
        "min_duration_s": 270,
        "max_duration_s": 330,
        "target_duration_s": 300,
        "min_words": 950,
        "max_words": 1160,
        "target_words": 1050,
        "body_sections_min": 2,
        "body_sections_max": 2,
        "require_subscribe_ask": True,
        "require_full_sections": True,
    },
    "full": {
        "label": "20-35 minute full explainer",
        "min_duration_s": 1200,
        "max_duration_s": 2100,
        "target_duration_s": 1500,
        "min_words": 4100,
        "max_words": 7550,
        "target_words": 5250,
        "body_sections_min": 10,
        "body_sections_max": 13,
        "require_subscribe_ask": True,
        "require_full_sections": True,
    },
}


@dataclass
class ThemeConfig:
    """Unified glassmorphic graphic look across every overlay element."""

    name: str = "vice-neon"
    panel_bg: str = "#1E1322"
    border_color: str = "#F7D3EA"
    glow_rgb: tuple[int, int, int] = (255, 120, 205)
    gradient_start: str = "#FF93DC"
    gradient_end: str = "#FFB27C"
    pill_pink: str = "#FB96E6"
    pill_cream: str = "#FFF1DE"
    pill_peach: str = "#FFBA90"
    pill_text: str = "#1A1020"
    font_display: str = "Montserrat"
    blur_px: int = BLUR_RADIUS_PX
    derived_from_key_art: str | None = None

    def to_css_vars(self) -> dict[str, str]:
        r, g, b = self.glow_rgb
        return {
            "--vc-panel-bg": self.panel_bg,
            "--vc-border": self.border_color,
            "--vc-glow-rgb": f"{r}, {g}, {b}",
            "--vc-grad-start": self.gradient_start,
            "--vc-grad-end": self.gradient_end,
            "--vc-pill-pink": self.pill_pink,
            "--vc-pill-cream": self.pill_cream,
            "--vc-pill-peach": self.pill_peach,
            "--vc-pill-text": self.pill_text,
            "--vc-font": f"'{self.font_display}', 'Arial Black', sans-serif",
            "--vc-blur": f"{self.blur_px}px",
        }


@dataclass
class VoiceConfig:
    """Voice synthesis configuration. API keys are NEVER stored in YAML or printed."""

    provider: str = "kokoro"  # "kokoro" (free local), "elevenlabs" (paid), "mock"
    voice_id: str = "af_heart"  # Kokoro local default or ElevenLabs voice ID
    api_key_file: str | None = None  # Path to local key file; read strictly on demand
    speed: float = 1.0
    language: str = "en-us"
    whisper_model: str = "large-v3-turbo"
    whisper_compute_type: str = "int8"

    def load_api_key(self, project_dir: Path | None = None) -> str:
        """Read ElevenLabs API key from file without ever logging or printing it."""
        if not self.api_key_file:
            raise ValueError(
                "ElevenLabs requires `voice.api_key_file` pointing to a local secret file "
                "(e.g. ~/.config/vicecut/elevenlabs.key). Never store raw keys in YAML."
            )
        key_path = Path(self.api_key_file).expanduser()
        if not key_path.is_absolute() and project_dir is not None:
            key_path = project_dir / key_path
        if not key_path.exists():
            raise FileNotFoundError(f"API key file not found at {key_path}")
        key = key_path.read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError(f"API key file {key_path} is empty")
        return key


@dataclass
class MediaPreferences:
    """User-supplied footage paths and source blocklist/allowlist."""

    existing_footage: list[str] = field(default_factory=list)
    allowed_official_channels: list[str] = field(default_factory=list)
    blocked_sources: list[str] = field(default_factory=list)
    max_sources_per_10min: int = 4
    min_sources_per_10min: int = 2
    max_stills_ratio: float = 0.10


@dataclass
class ApprovalState:
    """Explicit human sign-off checkpoints enforced by the pipeline."""

    script_approved: bool = False
    tts_cost_approved: bool = False
    tts_estimated_chars: int = 0
    storyboard_approved: bool = False
    qc_rounds_completed: int = 0


@dataclass
class ProjectBrief:
    """Complete project configuration generated from the 6 intake inputs."""

    slug: str
    topic: str  # "everything you need to know about ___ before you ___"
    subject: str
    action: str  # play, buy, watch, visit, file, switch
    viewer: str
    length_preset: str = "sample"  # sample | medium | full
    research_date: str = field(default_factory=lambda: date.today().isoformat())
    motif: str = ""
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    media: MediaPreferences = field(default_factory=MediaPreferences)
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    approvals: ApprovalState = field(default_factory=ApprovalState)

    @property
    def preset_spec(self) -> dict[str, Any]:
        if self.length_preset not in LENGTH_PRESETS:
            raise ValueError(
                f"Unknown length_preset '{self.length_preset}'. "
                f"Choose from: {', '.join(LENGTH_PRESETS)}"
            )
        return LENGTH_PRESETS[self.length_preset]

    def required_fact_count(self) -> int:
        """One usable confirmed fact per 16 seconds of target runtime."""
        target_s = self.preset_spec["target_duration_s"]
        return max(4, round(target_s / 16.0))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["theme"]["glow_rgb"] = list(self.theme.glow_rgb)
        return data

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "ProjectBrief":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        voice_raw = raw.get("voice", {})
        media_raw = raw.get("media", {})
        theme_raw = raw.get("theme", {})
        if "glow_rgb" in theme_raw and isinstance(theme_raw["glow_rgb"], list):
            theme_raw["glow_rgb"] = tuple(int(v) for v in theme_raw["glow_rgb"][:3])
        approvals_raw = raw.get("approvals", {})
        return cls(
            slug=raw["slug"],
            topic=raw["topic"],
            subject=raw.get("subject", ""),
            action=raw.get("action", "play"),
            viewer=raw.get("viewer", ""),
            length_preset=raw.get("length_preset", "sample"),
            research_date=raw.get("research_date", date.today().isoformat()),
            motif=raw.get("motif", ""),
            voice=VoiceConfig(**voice_raw),
            media=MediaPreferences(**media_raw),
            theme=ThemeConfig(**theme_raw),
            approvals=ApprovalState(**approvals_raw),
        )


# ---------------------------------------------------------------------------
# Key-Art Neighbouring-Hue Palette Extractor
# ---------------------------------------------------------------------------
def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(
        max(0, min(255, int(rgb[0]))),
        max(0, min(255, int(rgb[1]))),
        max(0, min(255, int(rgb[2]))),
    )


def _hsv_to_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return _rgb_to_hex((round(r * 255), round(g * 255), round(b * 255)))


def _hsv_to_rgb255(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return (round(r * 255), round(g * 255), round(b * 255))


def extract_palette_from_key_art(image_path: Path, name: str = "key-art") -> ThemeConfig:
    """
    Extract two neighbouring vibrant hues from subject key art and build a
    matching near-black glass panel + pale border + glow + source pills.
    Used when the subject is not warm and neon (per <direction>).
    """
    img = Image.open(image_path).convert("RGB")
    img.thumbnail((160, 160))
    pixels = np.asarray(img, dtype=np.float32).reshape(-1, 3) / 255.0

    # Convert RGB pixels to HSV using vectorized math
    cmax = pixels.max(axis=1)
    cmin = pixels.min(axis=1)
    delta = cmax - cmin

    sat = np.where(cmax > 1e-6, delta / np.maximum(cmax, 1e-6), 0.0)
    val = cmax

    # Filter to chromatic, non-shadow, non-blown-out pixels
    mask = (sat > 0.25) & (val > 0.18) & (val < 0.96)
    if np.count_nonzero(mask) < 20:
        mask = sat > 0.10
    selected = pixels[mask] if np.count_nonzero(mask) > 0 else pixels

    hues = []
    weights = []
    for r, g, b in selected:
        h, s, v = colorsys.rgb_to_hsv(float(r), float(g), float(b))
        hues.append(h)
        weights.append(s * v)

    # 36-bin hue histogram (10 deg per bin)
    bins = 36
    hist, _ = np.histogram(hues, bins=bins, range=(0.0, 1.0), weights=weights)
    dominant_bin = int(np.argmax(hist))
    h1 = (dominant_bin + 0.5) / bins

    # Pick strongest neighbouring hue within 20-60 degrees (2..6 bins away)
    best_neighbor_bin = (dominant_bin + 3) % bins
    best_neighbor_val = -1.0
    for offset in (-6, -5, -4, -3, -2, 2, 3, 4, 5, 6):
        b_idx = (dominant_bin + offset) % bins
        if hist[b_idx] > best_neighbor_val:
            best_neighbor_val = float(hist[b_idx])
            best_neighbor_bin = b_idx
    h2 = (best_neighbor_bin + 0.5) / bins

    # Keep gradient bright and legible on dark glass, panel near-black in h1
    panel_bg = _hsv_to_hex(h1, 0.42, 0.13)
    border_color = _hsv_to_hex(h1, 0.18, 0.96)
    glow_rgb = _hsv_to_rgb255(h1, 0.55, 0.98)
    grad_start = _hsv_to_hex(h1, 0.48, 0.99)
    grad_end = _hsv_to_hex(h2, 0.46, 0.99)
    pill_1 = _hsv_to_hex(h1, 0.44, 0.98)
    pill_2 = _hsv_to_hex((h1 + h2) / 2.0, 0.14, 0.99)
    pill_3 = _hsv_to_hex(h2, 0.44, 0.98)
    pill_text = _hsv_to_hex(h1, 0.48, 0.11)

    return ThemeConfig(
        name=name,
        panel_bg=panel_bg,
        border_color=border_color,
        glow_rgb=glow_rgb,
        gradient_start=grad_start,
        gradient_end=grad_end,
        pill_pink=pill_1,
        pill_cream=pill_2,
        pill_peach=pill_3,
        pill_text=pill_text,
        derived_from_key_art=str(image_path),
    )


def project_dirs(project_root: Path) -> dict[str, Path]:
    """Standard per-video folder layout required by <build> #1."""
    return {
        "root": project_root,
        "research": project_root / "research",
        "script": project_root / "script",
        "media": project_root / "media",
        "media_raw": project_root / "media" / "raw",
        "media_scenes": project_root / "media" / "scenes",
        "media_stills": project_root / "media" / "stills",
        "media_headlines": project_root / "media" / "headlines",
        "media_photos": project_root / "media" / "photos",
        "voice": project_root / "voice",
        "voice_takes": project_root / "voice" / "takes",
        "voice_aligned": project_root / "voice" / "aligned",
        "voice_master": project_root / "voice" / "master",
        "timeline": project_root / "timeline",
        "renders": project_root / "renders",
        "renders_frames": project_root / "renders" / "frames",
        "renders_proofs": project_root / "renders" / "card_proofs",
    }
