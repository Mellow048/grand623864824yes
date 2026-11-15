# ViceCut QC Report — `gta6-before-you-play`

**Overall Status:** NEEDS FIXES

| Metric | Measured | Target | Status | Notes |
| :--- | :--- | :--- | :---: | :--- |
| Picture changes / min | 26.5/min | 25-31 / min | **PASS** |  |
| First-60s picture changes | 27.0/min | 24-29 in first 60s | **PASS** |  |
| Median shot duration | 2.27s | 1.9-2.4s | **PASS** |  |
| Shots over 6s | 0 (0.0%) | <= 3% (0 ideal) | **PASS** |  |
| Unique shots (zero repeats) | 0 repeats, 0 missing | 0 repeats, 0 missing | **PASS** |  |
| Card on-screen coverage | 36.4% | 35%-40% | **PASS** |  |
| Longest bare footage streak | 3.8s (0-60s) / 6.4s (60s+) | <= 5.0s / <= 7.0s | **PASS** |  |
| Official stills share | 0.0% | < 10% | **PASS** |  |
| Longest silence pause | 0.48s | <= 0.50s | **PASS** |  |
| Audio loudness | not yet mastered | -18 LUFS / LRA <= 3 / TP < -2 dB | **FAIL** | run `vicecut audio` |
| End voice/caption sync drift | 0.0 ms | <= 50 ms | **PASS** |  |
| Look-and-fix QC rounds | 0 rounds | >= 3 rounds | **FAIL** | inspect 4fps preview sheets & full-size card proofs 3 times |

## Honest Shortfalls
- **Audio loudness**: measured not yet mastered vs target -18 LUFS / LRA <= 3 / TP < -2 dB (run `vicecut audio`)
- **Look-and-fix QC rounds**: measured 0 rounds vs target >= 3 rounds (inspect 4fps preview sheets & full-size card proofs 3 times)
