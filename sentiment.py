"""
News sentiment analyser — zero external API dependencies.

Fetches Google News RSS for a given query, scores each headline
using weighted keyword lists, and returns a sentiment score in [-1, 1].

Score interpretation
--------------------
  > +0.15  Bullish  — news favours the YES outcome
  < -0.15  Bearish  — news goes against the YES outcome
  otherwise Neutral — not enough signal; defer to price strategy

Caching
-------
Results are cached per query for SENTIMENT_CACHE_TTL seconds (default 20 min)
to avoid hammering the RSS endpoint every 60-second bot loop.
"""

from __future__ import annotations

import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote_plus

from logger import get_logger

log = get_logger()

SENTIMENT_CACHE_TTL = int(os.getenv("SENTIMENT_CACHE_TTL", str(20 * 60)))  # seconds

# ---------------------------------------------------------------------------
# Keyword lists  (tuples of (word, weight))
# Positive = favours the YES outcome
# Negative = goes against the YES outcome
# ---------------------------------------------------------------------------

POSITIVE_KEYWORDS: list[tuple[str, float]] = [
    # Sports
    ("wins",        1.5), ("won",          1.5), ("victory",      1.5),
    ("champion",    2.0), ("champions",    2.0), ("title",        1.2),
    ("qualifies",   1.5), ("qualified",    1.5), ("advances",     1.2),
    ("dominates",   1.3), ("dominant",     1.2), ("unbeaten",     1.3),
    ("favorite",    1.0), ("favourite",    1.0), ("top form",     1.2),
    ("comeback",    0.8), ("comeback win", 1.2), ("clean sheet",  1.0),
    # General / finance
    ("surge",       1.2), ("surges",       1.2), ("rally",        1.2),
    ("soars",       1.2), ("soar",         1.2), ("rises",        0.8),
    ("beats",       1.0), ("beats expectations", 1.5),
    ("breakthrough",1.2), ("record",       0.8), ("high",         0.6),
    ("positive",    0.7), ("bullish",      1.3), ("leads",        1.0),
    ("ahead",       0.7), ("outperforms",  1.2), ("strong",       0.8),
    ("boosts",      1.0), ("upgrade",      1.0),
]

NEGATIVE_KEYWORDS: list[tuple[str, float]] = [
    # Sports
    ("loses",       1.5), ("lost",         1.5), ("defeat",       1.5),
    ("defeated",    1.5), ("eliminated",   2.0), ("elimination",  2.0),
    ("knocked out", 2.0), ("injury",       1.8), ("injured",      1.8),
    ("suspended",   1.5), ("suspension",   1.5), ("ban",          1.3),
    ("banned",      1.3), ("crisis",       1.2), ("underdog",     0.8),
    ("upset",       1.0), ("struggle",     0.8), ("struggles",    0.8),
    ("miss",        1.0), ("misses",       1.0), ("fired",        1.2),
    # General / finance
    ("crash",       1.5), ("crashes",      1.5), ("plunges",      1.5),
    ("falls",       1.0), ("drops",        1.0), ("declines",     0.8),
    ("scandal",     1.5), ("fraud",        1.8), ("investigation",1.2),
    ("bearish",     1.3), ("negative",     0.7), ("fails",        1.2),
    ("below expectations", 1.5),                ("downgrade",    1.0),
    ("warning",     0.8), ("concern",      0.7), ("risk",         0.6),
]


@dataclass
class SentimentResult:
    query: str
    score: float          # -1.0 to +1.0
    label: str            # "bullish" | "bearish" | "neutral"
    articles_found: int
    top_headlines: list[str]


class SentimentAnalyzer:
    """
    Fetches and scores Google News RSS for market-specific queries.
    Thread-safe for single-threaded use (the bot loop).
    """

    def __init__(self):
        # Cache: {query: (timestamp, SentimentResult)}
        self._cache: dict[str, tuple[float, SentimentResult]] = {}

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def analyse(self, query: str) -> SentimentResult:
        """
        Returns a SentimentResult for the query.
        Uses cache if result is fresh enough.
        """
        cached = self._cache.get(query)
        if cached and (time.time() - cached[0]) < SENTIMENT_CACHE_TTL:
            return cached[1]

        result = self._fetch_and_score(query)
        self._cache[query] = (time.time(), result)
        return result

    def should_buy(self, query: str, min_score: float = -0.15) -> bool:
        """
        Returns False (block BUY) only when sentiment is clearly bearish.
        Neutral is treated as OK to buy — price signal takes priority.
        """
        if not query:
            return True
        result = self.analyse(query)
        ok = result.score >= min_score
        log.info(
            "[SENTIMENT] %s | score=%.2f (%s) | %d articulos | BUY=%s",
            query[:40], result.score, result.label, result.articles_found,
            "OK" if ok else "BLOQUEADO",
        )
        return ok

    def should_sell(self, query: str, min_score: float = 0.15) -> bool:
        """
        Returns False (block SELL) only when sentiment is clearly bullish —
        i.e. hold the position a bit longer.
        """
        if not query:
            return True
        result = self.analyse(query)
        ok = result.score <= min_score
        log.info(
            "[SENTIMENT] %s | score=%.2f (%s) | %d articulos | SELL=%s",
            query[:40], result.score, result.label, result.articles_found,
            "OK" if ok else "RETENIENDO (noticias positivas)",
        )
        return ok

    # -----------------------------------------------------------------------
    # Fetch + score
    # -----------------------------------------------------------------------

    def _fetch_and_score(self, query: str) -> SentimentResult:
        headlines = self._fetch_headlines(query)
        if not headlines:
            return SentimentResult(query, 0.0, "neutral", 0, [])

        pos_total = 0.0
        neg_total = 0.0

        for headline in headlines:
            text = headline.lower()
            for word, weight in POSITIVE_KEYWORDS:
                if word in text:
                    pos_total += weight
            for word, weight in NEGATIVE_KEYWORDS:
                if word in text:
                    neg_total += weight

        total = pos_total + neg_total
        if total == 0:
            score = 0.0
        else:
            score = (pos_total - neg_total) / total   # range [-1, 1]

        score = max(-1.0, min(1.0, score))
        label = "bullish" if score > 0.15 else ("bearish" if score < -0.15 else "neutral")

        return SentimentResult(
            query=query,
            score=round(score, 4),
            label=label,
            articles_found=len(headlines),
            top_headlines=headlines[:3],
        )

    def _fetch_headlines(self, query: str) -> list[str]:
        """Fetches up to 10 headlines from Google News RSS."""
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PolymarketBot/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                xml_data = resp.read()

            root = ET.fromstring(xml_data)
            titles = []
            for item in root.iter("item"):
                title_el = item.find("title")
                desc_el  = item.find("description")
                if title_el is not None and title_el.text:
                    titles.append(title_el.text)
                elif desc_el is not None and desc_el.text:
                    titles.append(desc_el.text)
                if len(titles) >= 10:
                    break

            return titles

        except Exception as exc:
            log.warning("[SENTIMENT] Error al obtener RSS para '%s': %s", query, exc)
            return []
