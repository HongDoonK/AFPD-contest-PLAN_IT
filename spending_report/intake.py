"""원본 문서 읽기: FF 7번 현금흐름표와 원천징수영수증을 구조화해 저장값과 대조한다.

FF 칸에는 고객이 쓴 글자가 그대로 들어 있다. 빈칸과 '잘 모르겠음'은 0원이 아니라 미확인이고,
'60 정도', '350~390'은 추정이다. 이 구분은 Cell 하나로 다룬다.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from .hwp import HwpError, hwp_tables
from .rules import FF_BLOCK, FF_BLOCKS

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NUM = r"-?[\d,]+(?:\.\d+)?"
_AMOUNT = r"\d[\d,]*(?:\.\d+)?"


class DocumentUnavailable(RuntimeError):
    """문서를 읽을 수 없을 때. 보고서는 '미검증'으로 표시하고 계속 만든다."""


class Cell(NamedTuple):
    won: int | None   # 범위면 중간값
    low: int | None
    high: int | None
    status: str       # 기재 / 추정 / 미확인
    raw: str


def docx_tables(path: str | Path) -> list[list[list[str]]]:
    """DOCX의 모든 표(중첩 표 포함)를 행·셀 문자열로 돌려준다."""
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    tables = []
    for tbl in root.iter(_W + "tbl"):
        rows = []
        for tr in tbl.findall(_W + "tr"):
            cells = []
            for tc in tr.findall(_W + "tc"):
                paragraphs = ("".join(t.text or "" for t in p.iter(_W + "t")) for p in tc.findall(_W + "p"))
                cells.append("".join(paragraphs).strip())
            rows.append(cells)
        tables.append(rows)
    return tables


def _manwon(text: str) -> int:
    return int(Decimal(text.replace(",", "")) * 10_000)


def parse_cell(text: str | None) -> Cell:
    """FF 칸을 금액으로 읽는다. 단위는 만원이다."""
    raw = (text or "").strip()
    fuzzy = bool(re.search(r"쯤|정도|얼마|대략|약\s*\d", raw))
    status = "추정" if fuzzy else "기재"
    eok = re.search(r"(\d+(?:\.\d+)?)\s*억(?:\s*(\d+)\s*천)?", raw)
    if eok:
        won = int(Decimal(eok.group(1)) * 100_000_000) + int(eok.group(2) or 0) * 10_000_000
        return Cell(won, won, won, status, raw)
    span = re.search(rf"({_AMOUNT})\s*[~∼]\s*({_AMOUNT})", raw)
    if span:
        low, high = _manwon(span.group(1)), _manwon(span.group(2))
        return Cell((low + high) // 2, low, high, "추정", raw)
    single = re.search(_AMOUNT, raw)
    if single is None:
        return Cell(None, None, None, "미확인", raw)
    won = _manwon(single.group(0))
    return Cell(won, won, won, status, raw)


# ── FF 7번 현금흐름표 ────────────────────────────────────────
@dataclass
class CashflowDoc:
    income: list[tuple[str, Cell]] = field(default_factory=list)
    outflow: list[tuple[str, str, Cell]] = field(default_factory=list)  # (묶음, 항목, 값)
    total_income: Cell = parse_cell("")
    total_outflow: Cell = parse_cell("")
    unverified_income: Cell = parse_cell("")
    unverified_outflow: Cell = parse_cell("")


_INCOME_HEADER = re.compile(r"^(근로, 사업소득|사업매출|성과금|기타소득)")


def parse_section7_rows(rows: list[list[str]]) -> CashflowDoc:
    """표 형식. DOCX는 [수입, 묶음, 항목, 금액], HWP는 [수입, 수입값, 묶음, 항목, 금액]이다."""
    doc, block, pending = CashflowDoc(), None, None

    def flush():
        nonlocal pending
        if pending:
            doc.income.append((pending, parse_cell("")))
            pending = None

    for cells in rows:
        if len(cells) < 4 or cells[0] == "수입":
            continue
        left = cells[0]
        if left == "합계":
            flush()
            doc.total_income, doc.total_outflow = parse_cell(cells[1]), parse_cell(cells[-1])
            continue
        if left == "미확인 수입":
            doc.unverified_income, doc.unverified_outflow = parse_cell(cells[1]), parse_cell(cells[-1])
            continue
        if _INCOME_HEADER.match(left):
            flush()
            pending = left
        elif left and pending:
            doc.income.append((pending, parse_cell(left)))
            pending = None
        section, label, amount = cells[-3], cells[-2], cells[-1]
        if section in FF_BLOCKS:
            block = section
        if block and label:
            doc.outflow.append((block, label, parse_cell(amount)))
    flush()
    return doc


def parse_section7_lines(lines: list[str]) -> CashflowDoc:
    """PDF 텍스트 형식. 수입 칸과 지출 칸이 줄 단위로 섞여 나온다."""
    doc, block, pending, inside = CashflowDoc(), None, None, False
    for raw in lines:
        line = raw.strip()
        if "수입, 지출 현황" in line:
            inside = True
            continue
        if not inside:
            continue
        if "기타 현금흐름 정보" in line:
            break
        total = re.match(rf"^합계\s+({_NUM})만원\s+합계\s+({_NUM})만원", line)
        unverified = re.match(rf"^미확인 수입\s+({_NUM})만원\s+미확인 지출\s+({_NUM})만원", line)
        income_value = re.match(rf"^({_NUM})만원$", line)
        row = re.match(rf"^(.+?)\s+({_NUM})$", line)
        if total:
            doc.total_income, doc.total_outflow = parse_cell(total.group(1)), parse_cell(total.group(2))
        elif unverified:
            doc.unverified_income, doc.unverified_outflow = parse_cell(unverified.group(1)), parse_cell(unverified.group(2))
        elif line in FF_BLOCKS:
            block = line
        elif _INCOME_HEADER.match(line):
            pending = line
        elif income_value and pending:
            doc.income.append((pending, parse_cell(income_value.group(1))))
            pending = None
        elif row and block:
            doc.outflow.append((block, row.group(1), parse_cell(row.group(2))))
    return doc


def _pdf_lines(path: Path) -> list[str]:
    try:
        import pdfplumber
    except ModuleNotFoundError as exc:
        raise DocumentUnavailable("pdfplumber가 설치되지 않아 PDF를 읽지 못함") from exc
    with pdfplumber.open(path) as pdf:
        return [line for page in pdf.pages for line in (page.extract_text() or "").splitlines()]


def document_tables(path: str | Path) -> list[list[list[str]]]:
    """HWP·DOCX의 표. 읽을 수 없으면 DocumentUnavailable."""
    path = Path(path)
    if not path.exists():
        raise DocumentUnavailable(f"파일 없음: {path.name}")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return docx_tables(path)
    if suffix == ".hwp":
        try:
            return hwp_tables(path)
        except HwpError as exc:
            raise DocumentUnavailable(f"HWP를 읽지 못함: {exc}") from exc
    raise DocumentUnavailable(f"지원하지 않는 형식: {suffix}")


def read_cashflow_doc(path: str | Path) -> CashflowDoc:
    path = Path(path)
    if not path.exists():
        raise DocumentUnavailable(f"파일 없음: {path.name}")
    if path.suffix.lower() == ".pdf":
        return parse_section7_lines(_pdf_lines(path))
    table = next((t for t in document_tables(path) if any("예/적금" in c for row in t for c in row)), None)
    if table is None:
        raise DocumentUnavailable("현금흐름표를 찾지 못함")
    return parse_section7_rows(table)


def _norm(label: str) -> str:
    return re.sub(r"\(.*?\)|\s+", "", label)


def reconcile_cashflow(doc: CashflowDoc, rows: list[dict], income_rows: list[dict]) -> dict:
    """문서의 7번 표와 저장된 프로필을 행 단위로 비교한다. 문서에서 비어 있는 칸은 비교하지 않는다."""
    stored = {(FF_BLOCK[r["section"]], _norm(r["item"])): r["monthly_won"] for r in rows}
    differences, seen, blanks = [], set(), 0
    for block, label, cell in doc.outflow:
        key = (block, _norm(label))
        seen.add(key)
        if key not in stored:
            differences.append(f"프로필에 없는 행: {block} {label}")
        elif cell.won is None:
            blanks += 1
        elif stored[key] != cell.won:
            differences.append(f"{block} {label}: 문서 {cell.won:,}원, 저장 {stored[key]}원")
    differences += [f"문서에 없는 행: {block} {label}" for (block, label) in stored if (block, label) not in seen]

    stored_income = [r["amount_won"] for r in income_rows]
    doc_income = [cell.won for _, cell in doc.income]
    # 저장값이 빈 칸은 부정기 수입처럼 일부러 월 수입에서 뺀 경우라 비교하지 않는다
    if len(stored_income) != len(doc_income) or any(None not in (d, s) and d != s for d, s in zip(doc_income, stored_income)):
        differences.append(f"수입 칸: 문서 {doc_income}, 저장 {stored_income}")
    if doc.total_income.won is not None and doc.total_income.won != sum(v or 0 for v in stored_income):
        differences.append(f"수입 합계: 문서 {doc.total_income.won}, 저장 {sum(v or 0 for v in stored_income)}")
    if doc.total_outflow.won is not None and doc.total_outflow.won != sum(v or 0 for v in stored.values()):
        differences.append(f"지출 합계: 문서 {doc.total_outflow.won}, 저장 {sum(v or 0 for v in stored.values())}")
    return {"status": "불일치" if differences else "일치", "rows_checked": len(doc.outflow) + len(doc.income) - blanks,
            "rows_blank": blanks, "differences": differences}


# ── 근로소득 원천징수영수증 ─────────────────────────────────
# (키, 분류, 첫 칸 패턴, 값 칸 번호). 소득·비과세 표는 주현과 종전을 더한 합계 칸을 쓴다
WITHHOLDING_FIELDS = (
    ("급여", "소득", r"^⑬", 5), ("상여", "소득", r"^⑭", 5), ("소득합계", "소득", r"^⑯ 계", 5),
    ("비과세소득", "비과세", r"^⑳ 비과세", 4),
    ("결정세액_소득세", "세액", r"^73", 1), ("결정세액_지방소득세", "세액", r"^73", 2),
    ("종전납부_소득세", "세액", r"^74", 1), ("종전납부_지방소득세", "세액", r"^74", 2),
    ("기납부_소득세", "세액", r"^75", 1), ("기납부_지방소득세", "세액", r"^75", 2),
    ("납부특례_소득세", "세액", r"^76", 1),
    ("차감징수_소득세", "세액", r"^77", 1), ("차감징수_지방소득세", "세액", r"^77", 2),
    ("총급여", "소득공제", r"^㉑", 1), ("근로소득공제", "소득공제", r"^㉒", 1), ("근로소득금액", "소득공제", r"^㉓", 1),
    ("기본공제", "소득공제", r"^기본공제", 1), ("국민연금", "소득공제", r"^㉛", 1),
    ("건강보험료", "소득공제", r"^㉝ 건강", 1), ("고용보험료", "소득공제", r"^㉝ 고용", 1),
    ("특별소득공제계", "소득공제", r"^㉟", 1), ("차감소득금액", "소득공제", r"^㊱", 1),
    ("주택청약공제", "소득공제", r"^㊴ 주택청약", 1), ("신용카드공제", "소득공제", r"^㊶", 1),
    ("그밖의소득공제계", "소득공제", r"^㊻", 1),
    ("과세표준", "세액계산", r"^㊽", 1), ("산출세액", "세액계산", r"^㊾", 1), ("세액감면계", "세액계산", r"^54", 1),
    ("근로소득세액공제", "세액공제", r"^55", 1),
    ("퇴직연금공제", "세액공제", r"^59", 1),
    ("연금저축대상", "세액공제", r"^60 연금저축 공제대상", 1), ("연금저축공제", "세액공제", r"^60 연금저축 세액", 1),
    ("보장성대상", "세액공제", r"^61 보장성보험 공제대상", 1), ("보장성공제", "세액공제", r"^61 보장성보험 세액", 1),
    ("월세대상", "세액공제", r"^70 월세액 공제대상", 1), ("월세공제", "세액공제", r"^70 월세액 세액", 1),
    ("세액공제계", "세액공제", r"^71", 1), ("결정세액", "세액계산", r"^72", 1),
    ("실효세율", "세액계산", r"^실효세율", 1),
)

# 저장된 프로필 항목 ↔ 영수증 키
_STORED_TAX = {"과세표준": "과세표준", "산출세액": "산출세액", "결정세액(소득세)": "결정세액_소득세",
               "결정세액(지방소득세)": "결정세액_지방소득세", "기납부세액(소득세)": "기납부_소득세",
               "차감징수세액(소득세)": "차감징수_소득세", "국민연금보험료": "국민연금",
               "건강보험료(장기요양 포함)": "건강보험료", "고용보험료": "고용보험료",
               "연금저축 납입": "연금저축대상", "월세액 공제대상": "월세대상"}
_STORED_INCOME = {"총급여": "총급여", "비과세소득": "비과세소득", "근로소득금액": "근로소득금액"}


def _value(text: str) -> float | int | None:
    pct = re.match(r"^([\d.]+)%$", text)
    if pct:
        return float(pct.group(1))
    found = re.search(_NUM, text)
    return int(found.group(0).replace(",", "")) if found else None


def parse_withholding(path: str | Path) -> dict[str, dict]:
    path = Path(path)
    if not path.exists():
        raise DocumentUnavailable(f"파일 없음: {path.name}")
    rows = [row for table in docx_tables(path) for row in table if row and row[0]]
    fields = {}
    for key, category, pattern, column in WITHHOLDING_FIELDS:
        row = next((r for r in rows if re.match(pattern, r[0])), None)
        if row is not None and len(row) > column:
            fields[key] = {"value": _value(row[column]), "category": category, "source_ref": row[0]}
    if "총급여" not in fields:
        raise DocumentUnavailable("근로소득 원천징수영수증 형식이 아님")
    return fields


def parse_business_withholding(path: str | Path) -> dict[str, dict]:
    """사업소득 원천징수영수증. 월별 지급 표와 합계 행을 읽는다."""
    path = Path(path)
    if not path.exists():
        raise DocumentUnavailable(f"파일 없음: {path.name}")
    table = next((t for t in docx_tables(path) if t and t[0][:3] == ["지급일", "귀속 연월", "지급총액"]), None)
    if table is None:
        raise DocumentUnavailable("사업소득 원천징수영수증 형식이 아님")
    total = next(r for r in table if r[0] == "합계")
    months = [_value(r[2]) for r in table[1:] if r[0] != "합계"]
    item = lambda value, category, ref: {"value": value, "category": category, "source_ref": ref}
    return {"지급총액": item(_value(total[2]), "소득", "합계 지급총액"), "소득세": item(_value(total[4]), "세액", "합계 소득세"),
            "지방소득세": item(_value(total[5]), "세액", "합계 지방소득세"), "세액계": item(_value(total[6]), "세액", "합계 세액 계"),
            "월합계": item(sum(months), "소득", "월별 지급총액 합"), "월최소": item(min(months), "소득", "월별 지급총액"),
            "월최대": item(max(months), "소득", "월별 지급총액"), "지급월수": item(len(months), "소득", "월별 지급 행 수")}


def read_withholding(path: str | Path) -> tuple[str, dict[str, dict]]:
    """영수증 종류를 알아내 (근로|사업, 항목)을 돌려준다."""
    try:
        return "근로", parse_withholding(path)
    except DocumentUnavailable:
        return "사업", parse_business_withholding(path)


def withholding_arithmetic(fields: dict[str, dict]) -> list[dict]:
    """영수증 안의 계산이 맞는지 확인한다."""
    v = lambda key: fields[key]["value"]
    checks = [
        ("급여 + 상여 = 총급여", v("급여") + v("상여"), v("총급여")),
        ("총급여 − 근로소득공제 = 근로소득금액", v("총급여") - v("근로소득공제"), v("근로소득금액")),
        ("근로소득금액 − 기본공제 − 국민연금 − 특별소득공제 = 차감소득금액",
         v("근로소득금액") - v("기본공제") - v("국민연금") - v("특별소득공제계"), v("차감소득금액")),
        ("차감소득금액 − 그 밖의 소득공제 = 과세표준", v("차감소득금액") - v("그밖의소득공제계"), v("과세표준")),
        ("산출세액 − 세액감면 − 세액공제 = 결정세액", v("산출세액") - v("세액감면계") - v("세액공제계"), v("결정세액")),
        ("결정세액 − 종전·주현 기납부 − 납부특례 = 차감징수세액",
         v("결정세액_소득세") - v("종전납부_소득세") - v("기납부_소득세") - v("납부특례_소득세"), v("차감징수_소득세")),
    ]
    return [{"check": name, "computed": computed, "document": document, "ok": computed == document}
            for name, computed, document in checks]


def reconcile_withholding(fields: dict[str, dict], tax_items: list[dict], income_items: list[dict]) -> dict:
    differences = [f"영수증 계산 불일치: {c['check']}" for c in withholding_arithmetic(fields) if not c["ok"]]
    checked = 0
    pairs = [(t["item"], t["amount_won"], _STORED_TAX.get(t["item"])) for t in tax_items]
    pairs += [(i["basis"], i["amount_won"], _STORED_INCOME.get(i["basis"])) for i in income_items if i["period"] == "연"]
    for name, stored, key in pairs:
        if key is None or key not in fields:
            continue
        checked += 1
        if fields[key]["value"] != stored:
            differences.append(f"{name}: 문서 {fields[key]['value']:,}원, 저장 {stored:,}원")
    categories: dict[str, int] = {}
    for f in fields.values():
        categories[f["category"]] = categories.get(f["category"], 0) + 1
    return {"status": "불일치" if differences else "일치", "fields_parsed": len(fields), "rows_checked": checked,
            "classified": categories, "differences": differences}


def reconcile_business(fields: dict[str, dict]) -> dict:
    v = lambda key: fields[key]["value"]
    differences = []
    if v("월합계") != v("지급총액"):
        differences.append(f"월별 합 {v('월합계'):,}원과 지급총액 {v('지급총액'):,}원이 다름")
    if v("소득세") + v("지방소득세") != v("세액계"):
        differences.append("소득세 + 지방소득세 ≠ 세액 계")
    return {"status": "불일치" if differences else "일치", "fields_parsed": len(fields), "rows_checked": v("지급월수") + 1,
            "classified": {"소득": 5, "세액": 3}, "differences": differences}
