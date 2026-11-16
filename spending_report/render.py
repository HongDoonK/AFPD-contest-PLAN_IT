"""module_result JSON → HTML 보고서. 형태는 templates/report.html.j2 한 파일에서 고친다."""
from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .narrative import won_text
from .rules import FIELD_LABELS


def manwon(won: int | None) -> str:
    """FF 표와 같은 만원 단위. 소수는 필요한 만큼만."""
    if won is None:
        return "미확인"
    text = f"{won / 10_000:,.3f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def cell_text(row: dict, key: str = "before_won") -> str:
    """표 칸 표시. 빈칸은 미확인, 범위는 그대로, 어림값은 ≈를 붙인다."""
    won = row.get(key)
    if won is None:
        return "미확인"
    if row.get("not_applicable"):
        return "해당없음"
    if row.get("low_won") is not None:
        return f"{manwon(row['low_won'])}~{manwon(row['high_won'])}"
    return ("≈" if row.get("estimated") else "") + manwon(won)


def change(won: int | None) -> str:
    if not won:
        return ""
    return ("+" if won > 0 else "−") + manwon(abs(won))


def reduction(won: int | None) -> str:
    return "" if not won else "−" + manwon(won)


def ymd(value: str | None) -> str:
    text = str(value or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if re.fullmatch(r"\d{8}", text) else text


def metric_value(value, unit: str) -> str:
    if value is None:
        return "계산 불가"
    if unit == "won":
        return won_text(value)
    if unit == "pct":
        return f"{value:g}%"
    if unit == "months":
        return f"{value:g}개월"
    return str(value)


_ENV = Environment(loader=FileSystemLoader(Path(__file__).with_name("templates")), autoescape=select_autoescape(["j2", "html"]),
                   trim_blocks=True, lstrip_blocks=True)
_ENV.filters.update(manwon=manwon, change=change, reduction=reduction, won=won_text, ymd=ymd,
                    field_label=lambda name: FIELD_LABELS.get(name, name))
_ENV.globals.update(metric_value=metric_value, cell_text=cell_text)


def to_html(report: dict) -> str:
    return _ENV.get_template("report.html.j2").render(r=report)
