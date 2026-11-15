# ViceCut Production Playbook (GTA 6 & General Faceless Explainers)

This playbook maps every production rule (`<inputs>`, `<start>`, `<direction>`, `<structure>`, `<build>` #1–#8, and `<gotchas>`) to exact `vicecut` commands and mandatory human approval gates.

---

## Stage 0 — Local Toolchain Check (`<build>` #1)

```bash
vicecut doctor
```

Install any missing free local tools before starting:
- Python 3.10+, `numpy`, `Pillow`, `PyYAML`
- `FFmpeg` + `ffprobe`
- `yt-dlp`
- `playwright` + `chromium` (`pip install playwright && playwright install chromium`)
- `faster-whisper` (`large-v3-turbo`, `int8`, CPU)
- `kokoro-onnx` (free local TTS) — or an ElevenLabs API key stored strictly in a local file (e.g., `.secrets/elevenlabs.key`, never committed or logged)
- Free SIL OFL **Montserrat Black (900)** and **Montserrat Bold (700)** fonts

---

## Stage 1 — Six-Question Intake (`<inputs>` & `<start>`)

Print the exact six-question intake prompt and wait for answers before doing any work:

```bash
vicecut intake
```

1. **Topic**: `"everything you need to know about ___ before you ___ (play, buy, watch, visit, file, switch)"` — or ask for 3 current pitches launching/peaking within 30 days with exact dates.
2. **Viewer**: Who is watching (e.g., PS5/Xbox/PC players weighing a GTA 6 pre-order).
3. **Length**: `sample` (60–90s, default first pass), `medium` (5m), or `full` (20–35m).
4. **Voice**: Free local Kokoro (default) or paid ElevenLabs with key read strictly from a local file path.
5. **Footage & Exclusions**: Any clips/images already owned + any blocked sources/topics.
6. **Look**: Two gradient colors (`#FF93DC` → `#FFB27C` default for GTA 6), or key-art path for automatic two-neighbouring-hue palette extraction.

Scaffold a dedicated per-video workspace once answers are locked:

```bash
vicecut init \
  --projects-dir projects \
  --slug gta6-launch-guide \
  --topic "everything you need to know about GTA 6 before you play" \
  --viewer "Console and PC players preparing for launch day" \
  --length sample \
  --preset presets/gta6.yaml \
  --voice-provider kokoro
```

Optional key-art color extraction: add `--key-art path/to/official_key_art.jpg`.

---

## Stage 2 — Fresh Same-Day Research & Fact Ledger (`<build>` #2)

- **Always research fresh on the day** — never copy another video's script, order, or jokes, and never trust memory.
- Open every source page yourself and record verbatim quotes, dates, and URLs in `research/facts.yaml`.
- Confirm every core claim across **2 independent sources** (`second_source_name` + `second_source_url`).
- Tag every fact with one `confidence` level: `official`, `on_record_interview`, `seen_by_previewer`, `single_source`, `sources_disagree`, `estimate`, `unknown`, or `false`.
- **Never** use leaks as evidence (leaks are allowed only when `is_debunk: true` and `confidence: false`).
- Aim for at least **1 verified fact per ~16s** of target runtime.

Audit the ledger and generate `research/sources.yaml`:

```bash
vicecut audit-research projects/gta6-launch-guide
```

---

## Stage 3 — Script Writing, Automated Linting & Gate 1 (`<structure>` & `<build>` #3)

Write `script/script.md` with **one sentence per line** and inline bracket tags (`[FACT:F01]`, `[SOURCE:Rockstar Games]`, `[CONFIDENCE:ESTIMATE]`, `[CARD:headline|id|outlet]`, `[CARD:photo|id|label]`, `[CARD:character|id|NAME;L1;L2;right]`, `[CARD:quote|line|ATTR]`, `[CARD:number|200 GB|SUBLINE]`, `[CAPTION:KEY PHRASE]`, `[SHOT:shot_01]`, `[TEASER:T1]`, `[PAYOFF:T1]`, `[END_PROMISE]`, `[HANDOFF]`, `[FLAG]`, `[BRIDGE]`, `[SO]`, `[JOKE]`, `[SUBSCRIBE]`, `[PLAN]`, `[HOT_TAKE]`, `[EITHER_OR]`, `[SIGNOFF]`, `[MOTIF]`).

Run the automated 28-rule script checker + pronunciation audit + TTS character count estimate:

```bash
vicecut check projects/gta6-launch-guide
```

Fix EVERY `[FAIL]` before proceeding. Confirm the pronunciation respeller (`script/pronunciations.yaml`) only affects voice synthesis and never alters on-screen captions/script text.

### Mandatory Human Approval Gate 1 (and Gate 2 for ElevenLabs)
1. Show the user the finished script, sources list, and estimated character count.
2. Wait for explicit approval:
   ```bash
   vicecut approve projects/gta6-launch-guide --gate script
   ```
3. If using ElevenLabs, show exact spoken character count (~1 credit/char) and wait for explicit yes:
   ```bash
   vicecut approve projects/gta6-launch-guide --gate tts-cost
   ```

---

## Stage 4 — Section Voice Recording, Whisper+Difflib Alignment & Audio Mastering (`<build>` #4)

Record one clean WAV per section (so any single bad line can be redone without re-recording the whole film):

```bash
vicecut voice projects/gta6-launch-guide
# Redo only section 2 if needed:
vicecut voice projects/gta6-launch-guide --sections 2
```

Run `faster-whisper` (`large-v3-turbo`, `int8`, CPU) + `difflib` forced word alignment (**never** trust raw TTS timestamps):

```bash
vicecut align projects/gta6-launch-guide
```

- Check the printed mismatch report. Re-record only flagged sections (real skips/misreads), never minor spelling/punctuation noise.

Master audio — trim true silences (`<-35 dB` down to `0.30s` with `35ms` pads on both sides, never blindly stripping natural breaths), apply speed capped at `1.12x` (`205–215 wpm` target), light compressor, and 2-pass `loudnorm` (`-18 LUFS`, `LRA <= 3 LU`, `TP < -2 dB`):

```bash
vicecut audio projects/gta6-launch-guide
```

---

## Stage 5 — Official Footage Search, Scene Cuts & Headlines (`<build>` #5)

List official publisher candidates **before** downloading anything (Rockstar Games, PlayStation, Xbox, Take-Two):

```bash
vicecut media-search "GTA 6 official trailer Rockstar Games" --project projects/gta6-launch-guide
```

After user confirms sources:
1. Download official clips (1080p+, clean of watermarks, never fan-made/concept/reupload/AI).
2. Split into clean scenes, run `detect_letterbox_crop()` on every clip (strip black bars/overlays), measure brightness (`compute_image_luminance()`), and build Start|Mid|End contact sheets (`build_scene_contact_sheet()`) to visually drop bad/menu/spoiler shots.
3. Capture article headlines via Playwright (`capture_headline_with_playwright()`), automatically falling back to clean white text cards whenever the headline fills `< 1/3` of the frame width or hits a paywall/cookie wall.
4. Register all verified clips, headlines, and official stills in `media/manifest.yaml` and verify shot budget (`>= 25 unique shots/min`, official stills `< 10%`):

```bash
vicecut media-budget projects/gta6-launch-guide
```

---

## Stage 6 — Word-Keyed Timeline, Overlay Stage & Pre-Render Storyboard Audit (`<build>` #6–#7)

Compile the deterministic 30fps word-keyed timeline, transparent `1920x1080` HTML overlay page, and pre-render storyboard sheet:

```bash
vicecut timeline projects/gta6-launch-guide
```

**Inspect `timeline/storyboard_sheet.png` BEFORE rendering a single frame of video.**
Verify that every caption matches the picture beneath it, bright/dark shots alternate cleanly, no card covers a face, and zero shots repeat.

Export full-size `1920x1080` single-frame PNG proofs of all 6 card types + captions + pills:

```bash
vicecut card-proofs projects/gta6-launch-guide
```

Record storyboard approval once verified:

```bash
vicecut approve projects/gta6-launch-guide --gate storyboard
```

---

## Stage 7 — 540p Preview, 4fps Contact Sheets & >= 3 Look-and-Fix Rounds (`<build>` #7–#8)

1. Render fast `540p` preview + automatic `4fps` contact sheets:
   ```bash
   vicecut render projects/gta6-launch-guide --mode preview
   ```
2. Visually inspect EVERY 4fps contact sheet + every full-size card proof PNG for:
   - Cut/card flicker (`0.1–0.2s` blur flash)
   - Caption/card collisions or face overlaps
   - Illegible headlines or letterbox black bars
   - Off-beat picture/caption pairing
3. Fix root causes in `script.md` / `media/manifest.yaml` / `timeline.py`, re-render preview, and log each completed visual inspection round (**minimum 3 rounds**):
   ```bash
   vicecut approve projects/gta6-launch-guide --gate qc-round
   ```

---

## Stage 8 — 1080p Master, <25 MB Phone Copy, QC Report & Credits (`<build>` #7–#8)

```bash
# 1080p 30fps master (hard cuts, 26px exact-frame blur/darken, transparent PNG overlay, -18 LUFS audio)
vicecut render projects/gta6-launch-guide --mode final

# Phone-sized shareable copy strictly under 25 MB
vicecut render projects/gta6-launch-guide --mode mobile

# Quantitative 12-metric QC table + Honest Shortfalls + full credits.md
vicecut qc projects/gta6-launch-guide
```

Deliver `renders/final_1080p.mp4`, `renders/mobile_under_25mb.mp4`, `renders/credits.md`, and `renders/qc_report.md` (with every shortfall reported honestly).
