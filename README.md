# ViceCut — Faceless GTA 6 & Explainer Video Production Framework

**ViceCut** (`src/vicecut/`) is a complete, deterministic, local-first Python production framework for making high-retention **1920×1080 30fps faceless explainer videos** tailored for **GTA 6 content production** — and configurable for any game, hardware launch, film, or topic.

It turns editorial craft, verification discipline, visual direction, audio mastering, and anti-flicker compositing rules into executable, lintable, unit-tested software:

- **100% Free Local Toolchain**: Python 3.10+, FFmpeg/ffprobe, `yt-dlp`, Playwright/Chromium, `faster-whisper` (`large-v3-turbo` `int8`), Kokoro local TTS (or optional ElevenLabs with file-only key security + credit approval gate), and SIL OFL Montserrat Black & Bold.
- **GTA 6 Vice City Broadcast Look**: Dark plum glass panels (`#1E1322`), `#F7D3EA` hairline borders, soft pink outer glow `(255, 120, 205)`, Montserrat Black uppercase gradient (`#FF93DC` → `#FFB27C`), stacked tilted source pills (`#FB96E6`, `#FFF1DE`, `#FFBA90`), 26px exact-frame backdrop blur + 40–60% darken — plus automatic two-neighbouring-hue extraction from any official key art for non-GTA topics.
- **Strict Editorial & Fact Guardrails**: 2-source confirmation ledger, confidence enums (`official` / `on_record_interview` / `seen_by_previewer` / `single_source` / `sources_disagree` / `estimate` / `unknown` / `false`), leak-as-evidence blocking, 28-rule automated script checker (`205–215 wpm`, banned phrases, Line 1 hook, `<= 10s` first fact, `>= 72%` 1-syllable words, `12–14w` mean sentence, `>= 28%` short sentences, `30–40%` `And`/`So` openers, `you`/`I`/`we` cadences, teaser/payoff verification, motif repetition).
- **Word-Keyed Anti-Flicker Timeline & Compositor**: Forced `faster-whisper` + `difflib` alignment, hard cuts only (~28/min, bright/dark alternating, zero shot repeats), cut-to-card snapping to prevent blur flicker, face-safe character card side flipping, card-caption collision prevention, bare-footage streak enforcement (`<= 5s` first min / `<= 7s` after), pre-render storyboard audit sheet, `540p` preview + `4fps` QC sheets, `1080p` master, `< 25 MB` phone copy, and 12-metric quantitative QC dashboard with honest shortfalls.

---

## One-Click Cloud Automation (Arena.ai Agent + GitHub Actions)

You don't have to install FFmpeg, Chromium, Whisper, or Kokoro on your own computer. Connect an **Arena.ai agent** to this GitHub repo:
1. Ask the agent to research fresh GTA 6 news, write and lint (`28/28`) your script, fill `media/fetch_plan.yaml` with approved official Rockstar/PlayStation/Xbox URLs, and push to GitHub.
2. Open **GitHub → Actions → `ViceCut — 1-Click Cloud Preview & QC` → Run workflow** (or let your agent trigger it with `gh workflow run vicecut-preview.yml -f project_dir=projects/<slug>`).
   - Auto-installs & caches FFmpeg, `yt-dlp`, Playwright Chromium, `faster-whisper`, Kokoro v1.0 AI voice, and Montserrat Black & Bold fonts.
   - Downloads official footage, splits into 1.7–3.5s clips, captures headlines, voices, aligns, masters audio to `-18 LUFS`, renders `preview_540p.mp4` + 4fps QC sheets + full-size 1080p card proofs, and commits the lightweight QC sheets into `<project>/qc_review/` so your Arena agent can visually inspect them and iterate!
3. Once approved, run **`ViceCut — 1-Click 1080p Master & Mobile Export`** to download `final_1080p.mp4`, `< 25 MB` `mobile_under_25mb.mp4`, `credits.md`, and `qc_report.md` (with optional Draft GitHub Release publishing).

Optional GitHub Secrets (**Settings → Secrets and variables → Actions**):
- `ELEVENLABS_API_KEY` — optional (only if using ElevenLabs instead of free Kokoro voice).
- `YTDLP_COOKIES_B64` — optional base64-encoded `cookies.txt` if YouTube age-gates or bot-checks cloud downloads.

---

## Quickstart (Local Machine)

### 1. Install the Package
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Optional extras for full local rendering & voice synthesis:
pip install -e ".[render,voice]"
playwright install chromium
```

### 2. Inspect Your Local Toolchain
```bash
vicecut doctor
```

### 3. Run the Included GTA 6 Sample Pipeline Right Now
The repository includes a complete 89-second GTA 6 sample workspace (`examples/gta6-sample/`) that passes all 28 automated script rules and timeline QC checks without needing FFmpeg or paid API keys:

```bash
vicecut audit-research examples/gta6-sample
vicecut check examples/gta6-sample
vicecut media-budget examples/gta6-sample
vicecut voice examples/gta6-sample
vicecut align examples/gta6-sample --synthetic
vicecut timeline examples/gta6-sample
vicecut render examples/gta6-sample --mode preview --dry-run --force
vicecut qc examples/gta6-sample
```

### 4. Preview All 6 Broadcast Card Types Interactively
Open [`docs/graphics_preview.html`](docs/graphics_preview.html) in any browser to live-inspect all 6 proof-card families (Headline, Photo, Character, Quote, Big Number, Chapter), non-shifting word-by-word captions, stacked top-left source pills, 26px blur/darken rules, and key-art palette presets at true 16:9 geometry.

---

## CLI Command Reference

| Command | Pipeline Stage | What It Does |
| :--- | :--- | :--- |
| `vicecut doctor` | `<build>` #1 | Checks Python, Pillow, numpy, FFmpeg, ffprobe, yt-dlp, Playwright, faster-whisper, Montserrat fonts, Kokoro & optional ElevenLabs key file. |
| `vicecut intake` | `<inputs>` / `<start>` | Prints the exact 6-question production intake brief. |
| `vicecut init --topic ... --viewer ...` | `<start>` | Scaffolds a self-contained per-video project folder under `--projects-dir` using `presets/gta6.yaml` or key-art palette extraction. |
| `vicecut audit-research <dir>` | `<build>` #2 | Audits `research/facts.yaml` for 2-source confirmation, confidence levels, leak blocking, blocked sources & 1-fact-per-16s density; writes `research/sources.yaml`. |
| `vicecut check <dir>` | `<structure>` / `<build>` #3 | Runs all 28 automated script checks, risky TTS pronunciation audit, and voice character/credit cost estimate. |
| `vicecut approve <dir> --gate ...` | Human Gates | Logs explicit human approval (`script`, `tts-cost`, `storyboard`, `qc-round`). |
| `vicecut voice <dir>` | `<build>` #4 | Synthesizes section-by-section WAV takes (`kokoro`, `elevenlabs` via local key file, or `mock`), enforcing approval gates. |
| `vicecut align <dir>` | `<build>` #4 | Runs `faster-whisper` (`large-v3-turbo` `int8`) + `difflib` forced word alignment (or `--synthetic` dry run) and flags sections needing re-recording. |
| `vicecut audio <dir>` | `<build>` #4 | Trims true silences (`<-35 dB` → `0.30s` w/ `35ms` pads), remaps word timestamps, caps tempo at `1.12x`, compresses & 2-pass loudnorms to `-18 LUFS`. |
| `vicecut media-search <query>` | `<build>` #5 | Lists `yt-dlp` official publisher candidates *before* downloading anything. |
| `vicecut media-download <dir>` | `<build>` #5 | Downloads an approved official trailer/press-kit clip via `yt-dlp` or direct URL (enforcing blocklist). |
| `vicecut media-split <dir>` | `<build>` #5 | Auto-splits official clips via FFmpeg scene detection into 1.7–3.5s muted 30fps shots, crops black bars, scores luminance & builds Start\|Mid\|End QC sheets. |
| `vicecut media-photo <dir>` | `<build>` #5 | Downloads an official press-kit image, measures dimensions & registers it in `media/manifest.yaml`. |
| `vicecut media-headline <dir>` | `<build>` #5 | Screenshots an article headline via Playwright with automatic white-card fallback if `< 1/3` frame wide or paywalled. |
| `vicecut media-fetch <dir>` | `<build>` #5 | Executes the entire declarative `media/fetch_plan.yaml` (download → split → photos → headlines) in one command. |
| `vicecut media-drop <dir>` | `<build>` #5 | Marks bad/logo/ESRB shots dropped after inspecting Start\|Mid\|End contact sheets. |
| `vicecut media-budget <dir>` | `<build>` #5 | Verifies unique shot pool (`~25 shots/min`) and official stills cap (`< 10%`). |
| `vicecut timeline <dir>` | `<build>` #6–#7 | Compiles deterministic 30fps `timeline.json`, `overlay_page.html`, and pre-render `storyboard_sheet.png`. |
| `vicecut card-proofs <dir>` | `<build>` #7 | Renders full-size `1920x1080` single-frame PNG proofs of every card type via Playwright. |
| `vicecut overlays <dir>` | `<build>` #7 | Exports transparent `1920x1080` overlay PNG frames (with 3x–5x lossless static-frame dedup & `--start-frame`/`--end-frame` chunking). |
| `vicecut render <dir> --mode ...` | `<build>` #7 | Runs FFmpeg hard-cut + exact-frame 26px blur/darken + RGBA overlay composite (`preview` 540p + 4fps QC sheets, `final` 1080p, `mobile` `< 25 MB`). |
| `vicecut qc <dir>` | `<build>` #8 | Measures all 12 quantitative QC metrics, writes `renders/qc_report.md` (including honest shortfalls) and `renders/credits.md`. |

---

## Repository Layout

```text
├── presets/
│   └── gta6.yaml                  # Flagship GTA 6 Vice Neon theme, Rockstar/PS/Xbox allowlist & phonetic lexicon
├── examples/
│   └── gta6-sample/               # Complete runnable 89s GTA 6 sample workspace passing 28/28 script checks
├── docs/
│   └── graphics_preview.html      # Interactive 1920x1080 browser playground for all 6 card types & captions
├── src/vicecut/
│   ├── config.py                  # Constants, theme tokens, file-only key loader, key-art palette extractor
│   ├── doctor.py                  # Local free toolchain diagnostics
│   ├── intake.py                  # 6-question intake, 30-day pitch validator & project scaffolder
│   ├── research.py                # Verified 2-source fact ledger, confidence enum & leak blocker
│   ├── pronunciation.py           # Voice-only TTS phonetic respeller (leaves on-screen text untouched)
│   ├── script_parser.py           # One-sentence-per-line annotated markdown script parser
│   ├── script_checker.py          # 28-rule automated structural, stylistic & sourcing script linter
│   ├── voice.py                   # Section-by-section Kokoro / ElevenLabs / Mock recorder + approval gates
│   ├── align.py                   # faster-whisper (large-v3-turbo int8) + difflib forced aligner
│   ├── audio.py                   # FFmpeg silence trim (0.30s + 35ms pad), <=1.12x tempo & 2-pass -18 LUFS loudnorm
│   ├── media.py                   # Official yt-dlp search, cropdetect, Start|Mid|End contact sheets & headline capture
│   ├── timeline.py                # Word-keyed hard-cut solver, anti-flicker snap, face-safe flip & bare-footage guard
│   ├── graphics.py                # Transparent 1920x1080 overlay compiler & Playwright frame/proof exporter
│   ├── templates/overlay.html     # Deterministic frame-indexed HTML/CSS/JS broadcast graphics engine
│   ├── render.py                  # Pre-render storyboard sheet, FFmpeg blur/darken compositor & <25 MB mobile encoder
│   ├── qc.py                      # 12-metric quantitative QC dashboard, Honest Shortfalls & credits.md generator
│   └── cli.py                     # Unified `vicecut` CLI (15 subcommands)
├── tests/                         # Comprehensive pytest suite covering every module & gotcha invariant
├── AGENT_PLAYBOOK.md              # Step-by-step operator SOP & human approval gates
└── pyproject.toml
```

---

## Running Tests

```bash
pytest -v
```

See [`AGENT_PLAYBOOK.md`](AGENT_PLAYBOOK.md) for the full end-to-end production workflow and visual QC checklists.
