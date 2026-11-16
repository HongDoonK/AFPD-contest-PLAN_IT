"""한국어 금액 표기를 원 단위 정수로 바꾸는 공용 파서.

"6천만원", "1억원", "3,500만원", "3,847천원", "154만원", "1억 2천만원", "138,780원"처럼
정책 원문에 섞여 나오는 표기를 모두 원(int)으로 환산한다.
"""
from __future__ import annotations

import re

_SMALL_UNITS = {"천": 1_000, "백": 100, "십": 10}
_BIG_UNITS = {"억": 100_000_000, "만": 10_000}

# 숫자 + (억|천만|백만|만|천)* + 원? 형태의 금액 덩어리
AMOUNT_PATTERN = r"\d[\d,]*(?:\.\d+)?\s*(?:억\s*(?:\d[\d,]*(?:\.\d+)?\s*)?)?(?:[천백십]?\s*만|천)?\s*원?"
AMOUNT_RE = re.compile(AMOUNT_PATTERN)


def parse_amount(text: str) -> int | None:
    """금액 문자열 하나를 원 단위 정수로 바꾼다. 단위가 전혀 없으면 None."""
    if not text:
        return None
    s = text.replace(",", "").replace(" ", "")
    if not re.search(r"[억만천원]", s):
        return None
    total = 0
    rest = s
    eok = re.match(r"(\d+(?:\.\d+)?)억(.*)", rest)
    if eok:
        total += float(eok.group(1)) * _BIG_UNITS["억"]
        rest = eok.group(2)
    man = re.match(r"(\d+(?:\.\d+)?)([천백십]?)만(.*)", rest)
    if man:
        mult = _SMALL_UNITS.get(man.group(2), 1)
        total += float(man.group(1)) * mult * _BIG_UNITS["만"]
        rest = man.group(3)
    else:
        cheon = re.match(r"(\d+(?:\.\d+)?)천원?(.*)", rest)
        if cheon:
            total += float(cheon.group(1)) * 1_000
            rest = cheon.group(2)
    won = re.match(r"(\d+(?:\.\d+)?)원", rest)
    if won:
        total += float(won.group(1))
    return int(round(total)) if total > 0 else None


def format_won(won: int | None) -> str:
    """원 → "1억 2,000만원" 같은 읽기 쉬운 표기."""
    if not won:
        return ""
    eok, man, rem = won // 100_000_000, (won % 100_000_000) // 10_000, won % 10_000
    if rem:
        return f"{won:,}원"
    parts = []
    if eok:
        parts.append(f"{eok:,}억")
    if man:
        parts.append(f"{man:,}만")
    return " ".join(parts) + "원"
