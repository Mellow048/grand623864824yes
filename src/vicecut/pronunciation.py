"""TTS pronunciation respeller: converts product names & numbers for voice while preserving on-screen text (<build> #3)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def int_to_spoken(n: int) -> str:
    """Convert an integer to natural spoken English (e.g. 212 -> 'two hundred and twelve')."""
    if n < 0:
        return "minus " + int_to_spoken(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, rem = divmod(n, 10)
        return _TENS[tens] if rem == 0 else f"{_TENS[tens]}-{_ONES[rem]}"
    if n < 1000:
        hundreds, rem = divmod(n, 100)
        head = f"{_ONES[hundreds]} hundred"
        return head if rem == 0 else f"{head} and {int_to_spoken(rem)}"
    if n < 1_000_000:
        thousands, rem = divmod(n, 1000)
        head = f"{int_to_spoken(thousands)} thousand"
        if rem == 0:
            return head
        conj = " and " if rem < 100 else " "
        return f"{head}{conj}{int_to_spoken(rem)}"
    millions, rem = divmod(n, 1_000_000)
    head = f"{int_to_spoken(millions)} million"
    return head if rem == 0 else f"{head} {int_to_spoken(rem)}"


def year_to_spoken(year: int) -> str:
    """Read years naturally: 2026 -> 'twenty twenty-six', 2005 -> 'two thousand and five'."""
    if 2000 <= year <= 2009:
        return int_to_spoken(year)
    if 1900 <= year <= 2099:
        hi, lo = divmod(year, 100)
        if lo == 0:
            return f"{int_to_spoken(hi)} hundred"
        if lo < 10:
            return f"{int_to_spoken(hi)} oh {_ONES[lo]}"
        return f"{int_to_spoken(hi)} {int_to_spoken(lo)}"
    return int_to_spoken(year)


DEFAULT_GTA6_OVERRIDES: dict[str, str] = {
    "GTA VI": "G T A six",
    "GTA 6": "G T A six",
    "GTA V": "G T A five",
    "GTA 5": "G T A five",
    "GTA Online": "G T A Online",
    "PS5 Pro": "P S five Pro",
    "PS5": "P S five",
    "Xbox Series X|S": "Xbox Series X and S",
    "Xbox Series X/S": "Xbox Series X and S",
    "Leonida": "Lee-oh-nee-dah",
    "RAGE": "Rage",
    "Take-Two": "Take Two",
    "PC": "P C",
    "DLC": "D L C",
    "NPC": "N P C",
    "NPCs": "N P Cs",
    "HDR": "H D R",
    "4K": "four K",
}


class PronunciationLexicon:
    """Applies voice-only phonetic respellings while keeping on-screen script intact."""

    def __init__(self, overrides: dict[str, str] | None = None):
        merged = dict(DEFAULT_GTA6_OVERRIDES)
        if overrides:
            merged.update(overrides)
        # Sort longest-first so 'PS5 Pro' replaces before 'PS5'
        self.overrides: dict[str, str] = dict(
            sorted(merged.items(), key=lambda kv: len(kv[0]), reverse=True)
        )

    @classmethod
    def load(cls, path: Path | None) -> "PronunciationLexicon":
        if path is None or not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(raw.get("overrides", {}))

    def to_spoken(self, text: str) -> str:
        """Transform written script text into TTS-safe phonetic text."""
        out = text

        # 1. Explicit lexicon replacements (case-sensitive word-boundary aware)
        for written, spoken in self.overrides.items():
            pattern = re.compile(r"(?<!\w)" + re.escape(written) + r"(?!\w)")
            out = pattern.sub(spoken, out)

        # 2. Dollar amounts ($80 -> eighty bucks, $1.5B -> one point five billion dollars)
        def _repl_currency(m: re.Match[str]) -> str:
            num_str = m.group("num").replace(",", "")
            suffix = (m.group("suffix") or "").upper()
            if "." in num_str:
                a, b = num_str.split(".", 1)
                spoken_num = f"{int_to_spoken(int(a))} point {' '.join(_ONES[int(d)] for d in b)}"
            else:
                spoken_num = int_to_spoken(int(num_str))
            if suffix == "B":
                return f"{spoken_num} billion dollars"
            if suffix == "M":
                return f"{spoken_num} million dollars"
            if suffix == "K":
                return f"{spoken_num} thousand dollars"
            return f"{spoken_num} bucks"

        out = re.sub(r"\$(?P<num>\d[\d,]*(?:\.\d+)?)(?P<suffix>[BMK])?\b", _repl_currency, out)

        # 3. Units people say naturally (210GB -> two hundred and ten gigs, 30fps -> thirty frames a second)
        out = re.sub(
            r"\b(\d+)\s*GB\b",
            lambda m: f"{int_to_spoken(int(m.group(1)))} gigs",
            out,
            flags=re.IGNORECASE,
        )
        out = re.sub(
            r"\b(\d+)\s*TB\b",
            lambda m: f"{int_to_spoken(int(m.group(1)))} terabytes",
            out,
            flags=re.IGNORECASE,
        )
        out = re.sub(
            r"\b(\d+)\s*fps\b",
            lambda m: f"{int_to_spoken(int(m.group(1)))} frames a second",
            out,
            flags=re.IGNORECASE,
        )
        out = re.sub(
            r"\b(\d+)%\b",
            lambda m: f"{int_to_spoken(int(m.group(1)))} percent",
            out,
        )

        # 4. Years (1990-2039)
        out = re.sub(
            r"\b(19\d{2}|20[0-3]\d)\b",
            lambda m: year_to_spoken(int(m.group(1))),
            out,
        )

        # 5. Remaining bare integers (e.g. 212 -> two hundred and twelve)
        def _repl_int(m: re.Match[str]) -> str:
            val = int(m.group(0).replace(",", ""))
            return int_to_spoken(val)

        out = re.sub(r"\b\d[\d,]*\b", _repl_int, out)
        return re.sub(r"\s+", " ", out).strip()

    def audit_risky_tokens(self, text: str) -> list[str]:
        """Find tokens in a script likely to be mispronounced if not in overrides."""
        risky: list[str] = []
        # All-caps acronyms (2-5 letters) not in overrides
        for tok in re.findall(r"\b[A-Z]{2,5}\b", text):
            if tok not in self.overrides and tok not in {"I", "A", "US", "UK", "OK"}:
                risky.append(tok)
        # Alphanumeric model codes like PS5, RTX4090, M4
        for tok in re.findall(r"\b[A-Za-z]+\d+[A-Za-z0-9]*\b", text):
            if tok not in self.overrides and not re.fullmatch(r"\d+(?:GB|TB|fps|ms|px|s)", tok, re.I):
                risky.append(tok)
        return sorted(set(risky))
