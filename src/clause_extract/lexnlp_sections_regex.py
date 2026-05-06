"""
Self-contained port of the regex-only path of
lexnlp.nlp.en.segments.sections from arthrod/lexpredict-lexnlp.

We port:
  - SECTION_TITLE_PTN / SECTION_TITLE_RE1 / SECTION_TITLE_RE2  (text-level break detection)
  - get_sections_re                                            (split text into sections by regex breaks)
  - SectionLevelParser                                          (assign abs_level and rel_level from title)
  - DocumentSection                                             (return type)

We do NOT port:
  - get_sections (ML path), build_section_break_features        — needs sklearn pickle
  - HeadingHeuristics                                            — only needed when reconciling sentence-level titles
  - get_section_spans / find_section_titles                     — orchestration we don't need here

Source: https://github.com/arthrod/lexpredict-lexnlp/blob/master/lexnlp/nlp/en/segments/sections.py
License: AGPLv3 (matches upstream LexPredict).
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import Any, ClassVar

import regex as re


# Verbatim from sections.py
SECTION_TITLE_PTN = r"""
\s*
(
    (?i:(?:appendix|part|section|article|(?:sub)?title|EXHIBIT|SCHEDULE)\s+)?
    (?-i:\d+(?:\.\d+)?
         |
         \p{Lu}(?:-\d+(?:\.\d+)?)?
         |
         [XVILCM]+
         |
         \((?:\d{1,3}|\p{L}+)\)
    )
)
(?:\.|\s|$)
"""

SECTION_TITLE_RE1 = re.compile(r"(?<=[,\.:>;\d\n\s]\n)" + SECTION_TITLE_PTN, re.M | re.X)
SECTION_TITLE_RE2 = re.compile(r"\A" + SECTION_TITLE_PTN, re.M | re.X)


@dataclass
class DocumentSection:
    start: int = 0
    end: int = 0
    title: str | None = ""
    title_start: int | None = 0
    title_end: int | None = 0
    level: int = 0
    abs_level: int = 0
    text: str = ""


def get_sections_re(text: str) -> Generator[str, None, None]:
    """Regex-based section split. Yields raw text sections."""
    prev_start = None
    for match in SECTION_TITLE_RE1.finditer(text):
        start = match.start()
        if prev_start is not None:
            yield text[prev_start:start]
        elif start != 0:
            yield text[0:start]
        prev_start = start
    if prev_start is not None:
        yield text[prev_start:]


class _Map(dict):
    """Lightweight stand-in for lexnlp.utils.map.Map."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.__dict__ = self


class SectionLevelParser:
    """Verbatim default hierarchy from sections.py."""

    DEFAULT_SECTION_HIERARCHY: ClassVar[list[str]] = [
        r"(?i:(appendix|exhibit|schedule|part|title)\s+\S+)",
        r"(?i:subtitle\s+\S+)",
        r"(?i:section\s+\S+)",
        r"(?i:subsection\s+\S+)",
        r"(?i:article\s+\S+)",
        r"\p{Lu}+(?:-\d+(?:\.\d+)?)?",
        r"[\d\.]+",
        r"\p{L}+(?:-\d+(?:\.\d+)?)?",
        r"\([\p{L}\d]+\)",
    ]

    def __init__(self, sections_hierarchy: list[str] | None = None):
        if not sections_hierarchy:
            sections_hierarchy = self.DEFAULT_SECTION_HIERARCHY
        self.default_sections_hierarchy = [
            _Map(regex=re.compile(i), abs_level=n, rel_level=None)
            for n, i in enumerate(sections_hierarchy, start=1)
        ]
        self.level = 1
        self.abs_level = 1
        self.prev_level_re = None
        self.title = ""

    @property
    def current_sections_hierarchy(self):
        return [i for i in self.default_sections_hierarchy if i.rel_level]

    def detect(self, title: str | None) -> tuple[int, int]:
        self.title = title or ""
        if not title:
            return 0, 0
        if self.prev_level_re and self.prev_level_re.match(title):
            return self.level, self.abs_level
        if self.current_sections_hierarchy:
            levels = self._get_from_detected()
            if levels is not None:
                return levels
        self._get_from_default()
        return self.level, self.abs_level

    def _get_from_detected(self) -> tuple[int, int] | None:
        for level_data in self.current_sections_hierarchy:
            if level_data.regex.match(self.title):
                self.level = level_data.rel_level
                self.abs_level = level_data.abs_level
                self.prev_level_re = level_data.regex
                return self.level, self.abs_level
        return None

    def _get_from_default(self) -> None:
        for level_data in self.default_sections_hierarchy:
            if level_data.regex.match(self.title):
                if level_data.abs_level > self.abs_level:
                    self.level += 1
                elif level_data.abs_level < self.abs_level:
                    self.level -= 1
                level_data.rel_level = self.level
                self.abs_level = level_data.abs_level
                self.prev_level_re = level_data.regex
                break


def section_spans_re(text: str) -> Generator[DocumentSection, None, None]:
    """Regex-only equivalent of lexnlp.get_section_spans(text, use_ml=False, return_text=True)."""
    _start = 0
    parser = SectionLevelParser()
    for section in get_sections_re(text):
        start_index = _start + text[_start:].index(section)
        end_index = start_index + len(section)
        _start = end_index
        try:
            title = SECTION_TITLE_RE2.findall(section)[0]
            title_start = start_index + section.index(title)
            title_end = title_start + len(title)
        except IndexError:
            title = None
            title_start = title_end = None
        level, abs_level = parser.detect(title)
        yield DocumentSection(
            start=start_index,
            end=end_index,
            title=title,
            title_start=title_start,
            title_end=title_end,
            level=level,
            abs_level=abs_level,
            text=section,
        )
