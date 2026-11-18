"""The glossary: one-line definitions shown when you hover a word on the digest page.

The words and definitions live in glossary.toml, written once and reviewed.
`Glossary` finds them in already-escaped HTML text and wraps them in a tip.
"""

from __future__ import annotations

import functools
import html
import re
import tomllib
from collections.abc import Mapping
from importlib import resources


@functools.cache
def load_glossary() -> dict[str, dict]:
    """The glossary in glossary.toml. Read once per process."""
    text = resources.files(__package__).joinpath("glossary.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)


class Glossary:
    """Finds glossary words in already-escaped HTML text and wraps them in a hover tip.

    Matches whole words, any case, longest first ("shelf registration" before
    "shelf"). Works on escaped text, so the words are escaped the same way before
    matching: "S&P 500" is looked for as "S&amp;P 500".
    """

    def __init__(self, entries: Mapping[str, Mapping]):
        self._key_by_word: dict[str, str] = {}
        for key, entry in entries.items():
            for word in entry["words"]:
                self._key_by_word[html.escape(word).lower()] = key
        self._definitions = {key: entry["definition"] for key, entry in entries.items()}
        words = sorted(self._key_by_word, key=len, reverse=True)
        # Not after a letter, digit, "-" or "&" (so never inside "&amp;"), and not
        # before a letter, digit or "-": "EPS" doesn't match inside "STEPS" or "EPS-1".
        self._pattern = re.compile(
            r"(?<![\w&-])(?:" + "|".join(map(re.escape, words)) + r")(?![\w-])",
            re.IGNORECASE,
        ) if words else None

    def keys_in(self, escaped: str) -> set[str]:
        """The glossary entries mentioned in this text."""
        if self._pattern is None:
            return set()
        return {self._key_by_word[m.group(0).lower()] for m in self._pattern.finditer(escaped)}

    def mark(self, escaped: str, seen: set[str]) -> str:
        """`escaped` with the first mention of each entry not in `seen` wrapped in a tip.
        Adds what it marks to `seen`, so a caller can share it across several texts."""
        if self._pattern is None:
            return escaped

        def wrap(m: re.Match) -> str:
            key = self._key_by_word[m.group(0).lower()]
            if key in seen:
                return m.group(0)
            seen.add(key)
            # tabindex: a tap (phone) or Tab key focuses it, which shows the tip too.
            definition = html.escape(self._definitions[key])
            return f'<span class="term" tabindex="0" data-tip="{definition}">{m.group(0)}</span>'

        return self._pattern.sub(wrap, escaped)


NO_GLOSSARY = Glossary({})  # marks nothing: for callers that don't want tips
