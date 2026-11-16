"""FF와 원천징수영수증을 읽어 케이스 JSON을 만들고 저장한다.

- 빈칸과 '잘 모르겠음'은 null + 미확인이다. 0으로 채우지 않는다.
- '60 정도', '350~390'은 추정이다. 범위는 low/high를 남기고 중간값을 쓴다.
- 7번 표가 비어 있어도 6번 표에 월 납입액이 있으면 가져오되 추정으로 표시한다.
- 문서 안의 문장은 자료로만 읽는다.

사용법:
    python -m customer.from_documents 가상고객데이터/빈칸형_가상고객_4명/01_김민지 --case-id blank_01
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from policy_enrich.amounts import AMOUNT_RE, parse_amount
from spending_report.intake import Cell, document_tables, parse_cell, parse_section7_rows, read_withholding

from .profile import connect, load_case, store, validate
from .region_map import REGION_CODE_MAP

ROOT = Path(__file__).resolve().parents[1]
_CITY_PREFIX = {"수원": "4111", "성남": "4113", "안양": "4117", "부천": "4119", "고양": "4128", "용인": "4146"}
_BANK_KINDS = ((r"세금", "세금준비금"), (r"청약", "주택청약"), (r"CMA|파킹", "파킹"), (r"적금", "적금"), (r"ISA", "ISA"),
               (r"예금", "정기예금"), (r"입출금|통장", "입출금"))
_SECURITY_KINDS = ((r"연금저축", "연금저축"), (r"퇴직연금|IRP", "퇴직연금"), (r"ETF|펀드", "ETF"), (r"주식", "주식"))
_LIQUIDITY = {"입출금": "즉시", "파킹": "즉시", "세금준비금": "즉시", "정기예금": "만기", "적금": "만기", "ISA": "만기",
              "주택청약": "해지손실", "연금저축": "해지손실", "퇴직연금": "해지손실", "ETF": "즉시", "주식": "즉시"}
_EARMARK = {"세금준비금": "세금 납부 준비", "주택청약": "주택청약 자격 유지", "연금저축": "노후", "퇴직연금": "노후"}
_SAVING_SLOTS = {"적금": "예/적금", "정기예금": "예/적금", "입출금": "예/적금", "파킹": "예/적금", "ISA": "예/적금",
                 "주택청약": "주택청약", "ETF": "펀드/ETF", "주식": "주식", "연금저축": "개인연금", "퇴직연금": "개인연금"}


def _fact(value, status=None, source_ref=None, assumptions=None, unit=None) -> dict:
    out = {"value": value}
    for key, item in (("status", status), ("source_ref", source_ref), ("assumptions", assumptions), ("unit", unit)):
        if item is not None:
            out[key] = item
    return out


def _table(tables: list, *headers: str) -> list[list[str]]:
    return next((t for t in tables if t and all(h in t[0] for h in headers)), [])


def _data_rows(table: list[list[str]]) -> list[list[str]]:
    return [r for r in table[1:] if any(c.strip() for c in r)]


def resolve_region(address: str) -> tuple[str | None, str | None, str | None, str | None]:
    """주소 → (코드, 시도, 시군구, 가정). 구를 모르면 시 단위 앞자리 코드를 추정으로 쓴다."""
    tokens = address.split()
    for sido, areas in REGION_CODE_MAP.items():
        if address.startswith(sido):
            for name, code in areas.items():
                if name in tokens:
                    return code, sido, name, None
    for city, prefix in _CITY_PREFIX.items():
        if address.startswith(city):
            return prefix, "경기", city, f"{city} 안의 구를 몰라 시 전체로 판정"
    return None, None, None, None


def _birth(cell: str, rrn: str | None) -> tuple[str | None, str | None, str | None]:
    found = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", cell)
    if found:
        return f"{found.group(1)}-{int(found.group(2)):02d}-{int(found.group(3)):02d}", "기재", None
    if rrn:
        century = 1900 if int(rrn[:2]) > 30 else 2000
        return f"{century + int(rrn[:2])}-{rrn[2:4]}-{rrn[4:6]}", "추정", "FF에 없어 원천징수영수증 주민번호 앞자리에서 가져옴"
    return None, "미확인", None


def _rrn(tables: list) -> str | None:
    found = re.search(r"(\d{6})-\*+", " ".join(c for t in tables for r in t for c in r))
    return found.group(1) if found else None


def _goal(row: list[str], index: int) -> dict:
    term, text = row[1], row[2]
    money = next((parse_amount(m.group(0)) for m in AMOUNT_RE.finditer(text) if parse_amount(m.group(0))), None)
    age, due = re.search(r"(\d{2})세", text), re.match(r"\s*(\d{2})\.(\d{2})", text)
    if age:
        return {"goal_id": f"g-{index}", "term": term, "name": "은퇴 생활비", "due": f"{age.group(1)}세", "amount_won": money,
                "amount_basis": "월 생활비", "value_basis": "미확인", "source_ref": "2. 재무목표", "raw": text.strip()}
    name = re.sub(r"^\s*\d{2}\.\d{2}\s*", "", text)
    name = re.sub(r"\d[\d,.]*\s*(?:억|만)\s*원?", "", name)
    return {"goal_id": f"g-{index}", "term": term, "name": " ".join(name.split()),
            "due": f"20{due.group(1)}-{due.group(2)}" if due else None, "amount_won": money, "amount_basis": "일시금",
            "value_basis": "미확인", "status": "기재" if money else "미확인", "source_ref": "2. 재무목표",
            "raw": text.strip()}


def _assets(tables: list) -> tuple[list[dict], list[tuple[str, Cell, str]]]:
    """자산 행과 (7번 칸, 월 납입액, 자산 id) 목록."""
    assets, monthly = [], []
    for i, row in enumerate(_data_rows(_table(tables, "예금주", "적립액"))):
        kind = next((k for p, k in _BANK_KINDS if re.search(p, row[0])), "입출금")
        balance, saving = parse_cell(row[6]), parse_cell(row[5])
        rate = re.search(r"([\d.]+)\s*%", row[7])
        asset_id = f"a-bank-{i}"
        assets.append({"asset_id": asset_id, "kind": kind, "name": row[0], "institution": row[4] or None, "balance_won": balance.won,
                       "monthly_contribution_won": saving.won, "rate_pct": float(rate.group(1)) if rate else None,
                       "earmark": _EARMARK.get(kind), "liquidity": _LIQUIDITY.get(kind), "status": balance.status,
                       "source_ref": "6-1) 은행상품", "assumptions": balance.raw or None})
        if saving.won is not None:
            monthly.append((_SAVING_SLOTS.get(kind, "예/적금"), saving, asset_id))
    for i, row in enumerate(_data_rows(_table(tables, "상품명", "평가액"))):
        kind = next((k for p, k in _SECURITY_KINDS if re.search(p, " ".join(row[:2]))), "주식")
        principal, value = parse_cell(row[2]), parse_cell(row[3])
        saving = re.search(r"월\s*(.+)", row[7])
        asset_id = f"a-sec-{i}"
        assets.append({"asset_id": asset_id, "kind": kind, "name": row[1] or row[0], "principal_won": principal.won, "balance_won": value.won,
                       "earmark": _EARMARK.get(kind), "liquidity": _LIQUIDITY.get(kind), "status": value.status,
                       "source_ref": "6-2) 증권상품", "assumptions": None if value.won is not None else "평가액 미기재"})
        if saving and parse_cell(saving.group(1)).won is not None:
            cell = parse_cell(saving.group(1))
            assets[-1]["monthly_contribution_won"] = cell.won
            monthly.append((_SAVING_SLOTS.get(kind, "펀드/ETF"), cell, asset_id))
    return assets, monthly


def _housing(table: list, assets: list[dict]) -> dict:
    row = table[1] if len(table) > 1 else [""] * 5
    tenure, amount = row[0], row[2]
    deposit = re.search(r"보증금\s*([^/]+)", amount)
    rent = re.search(r"월\s*(.+)", amount.split("/")[-1]) if "/" in amount else None
    facts = {"housing_tenure": _fact(tenure or None, source_ref="4. 주택계획"),
             "housing_type": _fact(row[1] or None, source_ref="4. 주택계획")}
    if tenure:
        facts["home_owner"] = _fact("자가" in tenure, source_ref="4. 주택계획")
    else:
        facts["home_owner"] = _fact(None)
    if deposit:
        cell = parse_cell(deposit.group(1))
        facts["rent_deposit"] = _fact(cell.won, unit="KRW", source_ref="4. 주택계획")
        assets.append({"asset_id": "a-deposit", "kind": "임차보증금", "balance_won": cell.won, "liquidity": "계약종료",
                       "status": cell.status, "source_ref": "4. 주택계획"})
    if rent:
        facts["rent_monthly"] = _fact(parse_cell(rent.group(1)).won, unit="KRW", source_ref="4. 주택계획")
    if "자가" in tenure:
        assets.append({"asset_id": "a-home", "kind": "자가주택", "balance_won": None, "liquidity": "계약종료", "status": "미확인",
                       "source_ref": "4. 주택계획", "assumptions": "시세·지분 미기재"})
    return facts


def _liabilities(table: list) -> list[dict]:
    out = []
    for i, row in enumerate(_data_rows(table)):
        balance = parse_cell(row[3])
        rate = re.search(r"([\d.]+)\s*%", row[7])
        out.append({"liability_id": f"l-{i}", "kind": row[0], "institution": row[1] or None, "principal_won": parse_cell(row[2]).won,
                    "balance_won": balance.won, "repayment": row[6] or None, "rate_pct": float(rate.group(1)) if rate else None,
                    "status": balance.status, "source_ref": "6-4) 대출 현황", "assumptions": row[7] or None})
    return out


def _insurance(table: list) -> tuple[list[dict], list[Cell]]:
    contracts, premiums = [], []
    for i, row in enumerate(_data_rows(table)):
        if not row[0]:
            continue
        premium = parse_cell(row[3])
        if "저축" not in row[0]:  # 저축성으로 적힌 보험은 7번 표의 보장성보험에 넣지 않는다
            premiums.append(premium)
        contracts.append({"contract_id": f"i-{i}", "name": row[0], "contractor": row[1] or None, "insured": row[2] or None,
                          "monthly_premium_won": premium.won, "coverage_summary": " ".join(c for c in row[4:7] if c) or None,
                          "started_on": row[7] or None, "pay_term": row[8] or None, "coverage_end": row[9] or None,
                          "status": premium.status, "source_ref": "6-3) 보험상품"})
    return contracts, premiums


def _cashflow(doc, memo: str, monthly: list, premiums: list[Cell], tenure: str) -> list[dict]:
    card_note = re.search(r"[^.]*카드값[^.]*\.", memo)
    rows = []
    for block, label, cell in doc.outflow:
        section = "보험" if label.startswith("보장성보험") else block
        row = {"section": section, "item": label, "monthly_won": cell.won, "low_won": cell.low, "high_won": cell.high,
               "status": cell.status, "source_ref": "7. 현금흐름표"}
        if label == "월 임대료" and cell.won is None and re.search(r"전세|자가", tenure):
            row.update(monthly_won=0, status="해당없음", assumptions=f"주거 형태가 {tenure}")
        if cell.raw and not re.fullmatch(r"[\d,.]+", cell.raw):
            row["detail"] = f"원문: {cell.raw}"
        if "카드값" in cell.raw:
            if card_note:
                row["detail"] += f" / {card_note.group(0).strip()}"
            row["purpose"] = "혼합" if card_note and re.search(r"업무|프로그램|노트북", card_note.group(0)) else "생활"
        rows.append(row)

    def fill(prefix: str, cell: Cell, source: str, asset_id: str | None = None) -> None:
        row = next((r for r in rows if r["item"].startswith(prefix) and r["monthly_won"] is None), None)
        if row:
            row.update(monthly_won=cell.won, low_won=cell.low, high_won=cell.high, status="추정", linked_asset_id=asset_id,
                       assumptions=f"7번 표가 비어 있어 {source}에서 가져옴")

    for slot, cell, asset_id in monthly:
        fill(slot, cell, "6번 자산표 월 납입액", asset_id)
    insurance_in_living = re.search(r"보험료는 생활비", memo)
    told = re.search(r"합쳐서\s*(\d+)\s*만", memo)
    known = [p for p in premiums if p.won is not None]
    if insurance_in_living:
        next(r for r in rows if r["section"] == "보험")["assumptions"] = "일부 보험료가 생활비에 들어 있어 합산하면 중복될 수 있음"
    elif premiums and len(known) == len(premiums):
        low, high = sum(p.low for p in known), sum(p.high for p in known)
        fill("보장성보험", Cell(sum(p.won for p in known), low, high, "추정", ""), "6번 보험표 월 보험료 합계")
    elif told:
        won = int(told.group(1)) * 10_000
        fill("보장성보험", Cell(won, won, won, "추정", ""), "메모의 합산 금액")
    return rows


def _income_and_tax(doc, kind: str, fields: dict, freelance_side: bool) -> tuple[list[dict], list[dict]]:
    main_kind = "근로" if kind == "근로" else "사업"
    income, tax = [], []
    basis_of = {"근로": "세후 입금", "사업": "세후 입금", "성과": "성과금", "기타": "기타소득"}
    for label, cell in doc.income:
        basis = basis_of[label[:2]] if label[:2] in basis_of else "세후 입금"
        note = f"원문: {cell.raw}" if cell.raw else None
        if basis == "기타소득" and re.search(r"있을 때|없는 달", cell.raw):  # 매달 들어오는 돈이 아니므로 월 수입에 더하지 않는다
            cell, note = Cell(None, None, None, "미확인", cell.raw), f"{note}. 부정기 수입이라 월 수입에 넣지 않음"
        income.append({"source_id": "현금흐름표", "kind": main_kind if basis == "세후 입금" else "기타", "basis": basis, "period": "월",
                       "tax_year": 2026, "amount_won": cell.won, "low_won": cell.low, "high_won": cell.high, "in_cashflow": True,
                       "status": cell.status, "source_ref": "7. 현금흐름표", "assumptions": note})
    v = lambda key: fields[key]["value"]
    if kind == "근로":
        for basis, key in (("총급여", "총급여"), ("비과세소득", "비과세소득"), ("근로소득금액", "근로소득금액")):
            income.append({"source_id": "근로", "kind": "근로", "basis": basis, "period": "연", "tax_year": 2025, "amount_won": v(key),
                           "status": "확인", "source_ref": fields[key]["source_ref"]})
        for item, key in (("과세표준", "과세표준"), ("산출세액", "산출세액"), ("결정세액(소득세)", "결정세액_소득세"),
                          ("결정세액(지방소득세)", "결정세액_지방소득세"), ("기납부세액(소득세)", "기납부_소득세"),
                          ("종전 근무지 기납부세액(소득세)", "종전납부_소득세"), ("차감징수세액(소득세)", "차감징수_소득세"),
                          ("국민연금보험료", "국민연금"), ("건강보험료(장기요양 포함)", "건강보험료"), ("고용보험료", "고용보험료"),
                          ("연금저축 납입", "연금저축대상"), ("월세액 공제대상", "월세대상")):
            tax.append({"tax_year": 2025, "filing": "연말정산", "item": item, "amount_won": v(key), "status": "확인",
                        "source_ref": fields[key]["source_ref"]})
    else:
        income.append({"source_id": "사업", "kind": "사업", "basis": "지급총액(원천징수영수증)", "period": "연", "tax_year": 2025,
                       "amount_won": v("지급총액"), "status": "확인", "source_ref": "합계 지급총액",
                       "assumptions": f"월 지급액 {v('월최소'):,}~{v('월최대'):,}원. 필요경비와 다른 지급처는 미확인"})
        for item, key in (("원천징수 소득세", "소득세"), ("원천징수 지방소득세", "지방소득세")):
            tax.append({"tax_year": 2025, "filing": "원천징수", "item": item, "amount_won": v(key), "status": "확인",
                        "source_ref": fields[key]["source_ref"]})
    if freelance_side:
        income.append({"source_id": "외주", "kind": "사업", "basis": "외주 소득", "period": "연", "tax_year": 2025, "amount_won": None,
                       "status": "미확인", "source_ref": "2. 기본정보 직업", "assumptions": "지급명세와 신고 여부 미확인"})
    return income, tax


def build_case(case_id: str, folder: Path, root: Path = ROOT) -> dict:
    ff_path = next(folder.glob("01_FACT_FINDING*"))
    receipt_path = next(folder.glob("02_*"))
    tables = document_tables(ff_path)
    receipt_tables = document_tables(receipt_path)
    kind, fields = read_withholding(receipt_path)
    relative = lambda p: p.resolve().relative_to(root.resolve()).as_posix()

    basic = _table(tables, "성명", "생년월일")
    memos = [t[0][0] for t in tables if len(t) == 1 and len(t[0]) == 1][2:]
    memo = " ".join(memos)
    stamp = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", memo)
    as_of = f"{stamp.group(1)}-{stamp.group(2)}-{stamp.group(3)}" if stamp else date.today().isoformat()

    address, occupation = basic[2][1], basic[5][3]
    birth, birth_status, birth_note = _birth(basic[1][2], _rrn(receipt_tables))
    code, sido, sigungu, region_note = resolve_region(address)
    marriage = _table(tables, "필요시기", "필요자금")
    marriage_row = marriage[1] if len(marriage) > 1 else [""] * 4
    marriage_text = " ".join(marriage_row) + " " + memo
    marital = "기혼" if re.search(r"기혼|배우자와", marriage_text) else "미혼" if "미혼" in marriage_text else None
    children = re.search(r"자녀\s*(\d)\s*명|아이\s*(한|\d)\s*명", marriage_text)
    if marital == "기혼" and children:
        household = 2 + (1 if children.group(2) == "한" else int(children.group(1) or children.group(2)))
    else:
        household = 1 if "혼자 살" in memo else None
    risk = _table(tables, "안정형", "성장형")
    risk_pick = next((risk[0][i] for i, c in enumerate(risk[1]) if c.strip() and c.strip() != "-"), None) if len(risk) > 1 else None
    employment = "프리랜서" if "프리랜서" in occupation else "재직" if kind == "근로" else None

    assets, monthly = _assets(tables)
    housing_facts = _housing(_table(tables, "현재 주거 구분"), assets)
    contracts, premiums = _insurance(_table(tables, "상품명", "계약자"))
    section7 = next(t for t in tables if any("예/적금" in c for r in t for c in r))
    doc = parse_section7_rows(section7)
    goals = [_goal(row, i) for i, row in enumerate(_table(tables, "필요시기", "재무목표")[1:], 1) if row[2].strip()]
    if "생각" in marriage_row[0] + marriage_row[1]:
        goals.append({"goal_id": "g-wedding", "term": "중기", "name": "결혼자금", "due": marriage_row[1] or None, "amount_won": None,
                      "amount_basis": "일시금", "value_basis": "미확인", "status": "미확인", "source_ref": "3. 결혼계획",
                      "raw": " ".join(c for c in marriage_row if c.strip())})
    income, tax = _income_and_tax(doc, kind, fields, "외주" in occupation)

    facts = {
        "doc_fact_finding": _fact(relative(ff_path)),
        "doc_withholding": _fact(relative(receipt_path)),
        "unverified_income": _fact(doc.unverified_income.won, unit="KRW", source_ref="7. 현금흐름표"),
        "unverified_outflow": _fact(doc.unverified_outflow.won, unit="KRW", source_ref="7. 현금흐름표"),
        "name": _fact(basic[1][1], source_ref="1. 기본정보"),
        "birth_date": _fact(birth, status=birth_status, source_ref="1. 기본정보", assumptions=birth_note),
        "sex": _fact(basic[1][3] or None, source_ref="1. 기본정보"),
        "address": _fact(address or None, source_ref="1. 기본정보"),
        "region_sido": _fact(sido, source_ref="1. 기본정보"),
        "region_sigungu": _fact(sigungu, source_ref="1. 기본정보"),
        "region_code": _fact(code, status="추정" if code else "미확인", assumptions=region_note or "주소의 시군구를 행정표준코드로 변환"),
        "occupation": _fact(occupation or None, source_ref="1. 기본정보"),
        "employment_status": _fact(employment, source_ref="1. 기본정보·영수증"),
        "employer_size": _fact(None) if kind == "근로" else _fact(None, status="해당없음"),
        "marital_status": _fact(marital, source_ref="3. 결혼계획·메모"),
        "household_size": _fact(household, status=None if household is None else "추정", unit="명", source_ref="메모",
                                assumptions=None if household is None else "메모의 가족 설명으로 판단"),
        "parents_household": _fact(None),
        "military_service": _fact(None),
        "national_pension_expected": _fact(None),
        "credit_score": _fact(int(re.search(r"\d+", basic[5][1]).group(0)) if re.search(r"\d+", basic[5][1]) else None, unit="점"),
        "risk_profile": _fact(risk_pick, source_ref="5. 투자성향"),
        "main_bank": _fact(basic[4][1] or None, source_ref="1. 기본정보"),
        "payday": _fact(basic[4][3] or None, source_ref="1. 기본정보"),
        **housing_facts,
    }
    # 결혼계획 칸에 '계획 없음'·'없음'·'비혼'이 적혀 있으면 그대로 남긴다(칸이 비면 넣지 않음)
    plan_cell = " ".join(c for c in (marriage_row[0], marriage_row[3] if len(marriage_row) > 3 else "") if c)
    if marital != "기혼" and re.search(r"계획\s*없음|없음|비혼|미정|아직", plan_cell):
        # 값은 원문 그대로 둔다. '당장 계획 없음'처럼 유보 표현이면 계획 단계에서 계획 없음으로 보지 않는다
        facts["marriage_plan"] = _fact(plan_cell.strip(), source_ref="3. 결혼계획", assumptions="결혼계획 칸 원문")
    child_cell = re.search(r"자녀\s*(?:계획)?\s*(?:은|는)?\s*(없음|계획\s*없음|안\s*낳)", memo + " " + plan_cell)
    if child_cell:
        facts["children_plan"] = _fact("없음", source_ref="메모·3. 결혼계획", assumptions=f"원문: {child_cell.group(0)}")
    return {
        "case_id": case_id, "profile_version": "v1", "as_of_date": as_of, "is_synthetic": True,
        "note": "문서에서 자동 생성. 빈칸은 미확인, 어림값은 추정으로 저장.",
        "default_source": relative(ff_path), "tax_source": relative(receipt_path), "cashflow_source": relative(ff_path),
        "facts": facts, "income_items": income, "tax_items": tax, "assets": assets, "liabilities": _liabilities(_table(tables, "금융기관", "잔액")),
        "cashflow_items": _cashflow(doc, memo, monthly, premiums, facts["housing_tenure"]["value"] or ""),
        "insurance_contracts": contracts, "goals": goals,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--out", default=Path("customer/cases/auto"), type=Path)
    parser.add_argument("--db", default=Path("data/customer_profiles.db"), type=Path)
    args = parser.parse_args()

    case = build_case(args.case_id, args.folder)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.case_id}.json"
    path.write_text(json.dumps(case, ensure_ascii=False, indent=1), encoding="utf-8")
    profile = load_case(path)
    issues = validate(profile)
    errors = [i for i in issues if i["level"] == "error"]
    if not errors:
        conn = connect(args.db)
        store(conn, profile)
        conn.close()
    rows = profile["cashflow_items"]
    print(json.dumps({"case": args.case_id, "file": str(path), "stored": not errors, "errors": errors,
                      "as_of": profile["as_of_date"], "region_code": case["facts"]["region_code"]["value"],
                      "cashflow_rows": len(rows), "rows_with_amount": sum(r["monthly_won"] is not None for r in rows),
                      "warnings": len(issues) - len(errors)}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
