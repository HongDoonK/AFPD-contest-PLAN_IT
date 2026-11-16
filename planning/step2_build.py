"""팀원 step2가 없는 고객용 step2 유도(만원).

프로필(customer.profile.load_case 결과)의 자산·현금흐름에서 팀원 step2와 같은 키·순서의 dict를 만든다.
- p 주택청약, q IRP·퇴직연금, r 연금저축, s 변액보험 잔액 합. M은 그 밖의 금융자산 합(임차보증금·부동산·해약환급금 제외).
- delta* = 월 납입 × 12, *_rate·*_date는 해당 자산에서(여럿이면 잔액 가중 이율, 가장 이른 가입일).
- deltaM = (월 소득 − 고정지출(보험 포함) − 변동지출) × 12. 지출 구분은 spending_cut의 step2 비교와 같다
  (변동 = '변동지출' 구역, 고정 = '고정지출'+'보험' 구역). 월 소득은 현금흐름 수입 행 합에서 '세금·사회보험' 구역을 뺀 값.
- spending_report가 주어지면 소비 조정액(calculations.effect.adjustment_monthly_won)을 변동지출에서 빼고 cut_monthly에 기록.
- 모르는 잔액·지출은 0으로 두고 경고, status는 INCOMPLETE.

사용법:
    python -X utf8 -m planning.step2_build --case customer/cases/auto/blank_01.json [--out <파일>]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from customer.profile import load_case

from .spending_cut import SECTIONS

LOCKED_KINDS = {
    "p": ("주택청약",),
    "q": ("IRP", "퇴직연금"),
    "r": ("연금저축",),
    "s": ("변액보험",),
}
LOCKED_NAMES = {"p": "주택청약", "q": "IRP", "r": "연금저축", "s": "변액보험"}
# M에 넣는 금융자산 종류. 이 밖의 종류(임차보증금, 자가주택, 부동산, 해약환급금 등)는 M에서 뺀다
M_KINDS = ("입출금", "정기예금", "예금", "적금", "ETF", "펀드", "주식", "CMA", "파킹", "ISA", "세금준비금",
           "MMF", "채권", "발행어음", "외화예금")
NON_FINANCIAL_KINDS = ("임차보증금", "자가주택", "부동산", "해약환급금", "전세보증금", "자동차")
VARIABLE_SECTIONS = ("변동지출",)
FIXED_SECTIONS = tuple(s for s in SECTIONS if s != "변동지출")  # 고정지출 + 보험
TAX_SECTION = "세금·사회보험"


def _man(won) -> float | None:
    return None if won is None else round(won / 10_000, 2)


def _num_text(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")


def classify_asset(asset: dict) -> str | None:
    """자산 행 → 'p'|'q'|'r'|'s'|'M'|None(비금융·제외)."""
    kind = str(asset.get("kind") or "")
    for key, kinds in LOCKED_KINDS.items():
        if kind in kinds:
            return key
    if kind in M_KINDS:
        return "M"
    return None


def _label(asset: dict) -> str:
    kind, name = asset.get("kind") or "", asset.get("name") or ""
    # 이름 끝의 숫자가 금액으로 읽히지 않게 숫자를 뺀 이름만 붙인다(allocation이 끝 숫자를 금액으로 읽음)
    name = re.sub(r"[\d,.]+", "", str(name)).strip()
    return kind if not name or name == kind else f"{kind} {name}"


def locked_and_m(profile: dict, warnings: list) -> dict:
    """자산을 p·q·r·s·M으로 나눈다. {'p': [...], ..., 'M': [...], 'excluded': [...]}"""
    groups = {k: [] for k in (*LOCKED_KINDS, "M")}
    excluded = []
    for a in profile.get("assets", []):
        key = classify_asset(a)
        if key is None:
            excluded.append(a)
            if a.get("kind") not in NON_FINANCIAL_KINDS:
                warnings.append(f"자산 종류 '{a.get('kind')}'를 분류하지 못해 M에서 뺌")
            continue
        groups[key].append(a)
    groups["excluded"] = excluded
    return groups


def _group_values(key: str, assets: list[dict], warnings: list, unknown: list) -> dict:
    name = LOCKED_NAMES.get(key, "M")
    balances = []
    for a in assets:
        if a.get("balance_won") is None:
            warnings.append(f"{_label(a)} 잔액 미확인 → 0으로 둠")
            unknown.append(f"{_label(a)} 잔액")
        balances.append(_man(a.get("balance_won")) or 0.0)
    total = float(round(sum(balances), 2))
    delta = None
    if assets and key != "M":  # M의 증가분은 현금흐름(deltaM)에서 구하므로 월 납입은 보지 않는다
        monthly = [a.get("monthly_contribution_won") for a in assets]
        if any(m is None for m in monthly):
            known = [m for m in monthly if m is not None]
            delta = round(sum(known) * 12 / 10_000, 2) if known else None
            warnings.append(f"{name} 월 납입액 일부 미확인 → delta{key}는 아는 값만 합산" if known
                            else f"{name} 월 납입액 미확인 → delta{key} = null")
            unknown.append(f"{name} 월 납입액")
        else:
            delta = round(sum(monthly) * 12 / 10_000, 2)
    rates = [(a.get("rate_pct"), b) for a, b in zip(assets, balances)]
    rate = None
    if rates and all(r is not None for r, _ in rates):
        weight = sum(b for _, b in rates)
        rate = round(sum(r * b for r, b in rates) / weight, 2) if weight else rates[0][0]
    dates = sorted(a["opened_on"] for a in assets if a.get("opened_on"))
    return {"value": total, "rate": rate, "date": dates[0] if dates else None, "delta": delta}


def _items(assets: list[dict], with_meta: bool) -> list[str]:
    out = []
    for a in assets:
        amount = _num_text(_man(a.get("balance_won")) or 0.0)
        if with_meta:
            extra = (f" @{a['rate_pct']:g}%" if a.get("rate_pct") is not None else "") + \
                    (f" 가입 {a['opened_on']}" if a.get("opened_on") else "")
            out.append(f"{_label(a)} {amount}{extra}")
        else:
            out.append(f"{_label(a)} {amount}")  # 끝 숫자 = 금액(allocation.m_available이 읽음)
    return out


def cashflow_totals(profile: dict, warnings: list, unknown: list) -> dict:
    """월 소득(세금·사회보험 차감)·고정(+보험)·변동 합계(만원)."""
    income_rows = [r for r in profile.get("income_items", []) if r.get("in_cashflow")]
    income = 0
    for r in income_rows:
        if r.get("amount_won") is None:
            unknown.append(f"수입 '{r.get('basis')}'")
        income += r.get("amount_won") or 0
    if not income_rows:
        warnings.append("현금흐름 수입 행이 없음 → 월 소득 0")
        unknown.append("월 소득")
    tax = 0
    sums = {"fixed": 0, "variable": 0}
    for r in profile.get("cashflow_items", []):
        sec = r.get("section")
        side = "variable" if sec in VARIABLE_SECTIONS else "fixed" if sec in FIXED_SECTIONS else None
        if sec == TAX_SECTION:
            if r.get("monthly_won") is None:
                unknown.append(f"세금 '{r.get('item')}'")
            tax += r.get("monthly_won") or 0
            continue
        if side is None:
            continue
        if r.get("monthly_won") is None:
            unknown.append(f"{sec} '{r.get('item')}'")
            continue
        sums[side] += r["monthly_won"]
    if tax:
        warnings.append(f"수입에서 세금·사회보험 월 {_man(tax):,.2f}만원을 빼 월 소득으로 씀(세후 가용소득 기준)")
    return {"income": _man(income - tax), "income_gross": _man(income), "tax": _man(tax),
            "fixed": _man(sums["fixed"]), "variable": _man(sums["variable"])}


def build_step2(profile: dict, spending_report: dict | None = None) -> dict:
    """프로필(만원 변환) → 팀원 step2 형식 dict. 입력은 바꾸지 않는다."""
    warnings: list[str] = []
    unknown: list[str] = []
    groups = locked_and_m(profile, warnings)
    vals = {k: _group_values(k, groups[k], warnings, unknown) for k in LOCKED_KINDS}
    m = _group_values("M", groups["M"], warnings, unknown)
    for c in profile.get("insurance_contracts", []):
        if "변액" in str(c.get("name") or "") and not groups["s"]:
            warnings.append(f"변액보험 계약 '{c.get('name')}'이 있으나 적립금이 자산표에 없음 → s = 0")
            unknown.append("변액보험 적립금")
    for a in groups["M"]:
        if a.get("earmark") and classify_asset(a) == "M":
            warnings.append(f"{_label(a)}는 용도 지정({a['earmark']}) 자산이지만 M에 포함")

    cf = cashflow_totals(profile, warnings, unknown)
    cut = 0.0
    cut_note = None
    if spending_report is not None:
        eff = (spending_report.get("calculations") or {}).get("effect") or {}
        adj = eff.get("adjustment_monthly_won")
        if adj is None:
            warnings.append("소비 보고서에 조정액이 없음 → cut_monthly 0")
        else:
            cut = _man(adj)
            if cut > cf["variable"]:
                warnings.append(f"소비 조정 {cut}만원이 변동지출 {cf['variable']}만원보다 큼 → 변동지출까지만")
                cut = cf["variable"]
            cut_note = "소비 보고서 조정액 전체를 변동지출에서 뺌(고정지출 항목 조정분 포함)"
    variable_after = round(cf["variable"] - cut, 2)
    a_monthly = round(cf["income"] - cf["fixed"] - variable_after, 2)
    status = "INCOMPLETE" if unknown else "OK"
    messages = []
    if unknown:
        messages.append("미확인 값은 0으로 계산: " + ", ".join(dict.fromkeys(unknown)))
    if cut_note:
        messages.append(cut_note)

    out = {}
    for k in LOCKED_KINDS:
        v = vals[k]
        out[k] = v["value"]
        out[f"{k}_rate"] = v["rate"]
        out[f"{k}_date"] = v["date"]
        out[f"delta{k}"] = v["delta"]
    out["M"] = m["value"]
    out["deltaM"] = round(a_monthly * 12, 2)
    out["_meta"] = {
        "client": None,
        "source_pdf": None,
        "unit": "금액 만원, delta*는 만원/년, *_rate는 연이율 %, *_date는 가입일 YYYY-MM-DD",
        "deltaM_detail": {
            "a_monthly": a_monthly,
            "income_monthly": cf["income"],
            "fixed_after_monthly": cf["fixed"],
            "variable_after_monthly": variable_after,
            "cut_monthly": cut,
            "status": status,
            "messages": messages,
        },
        "asset_detail": {
            **{f"{k}_items": _items(groups[k], True) for k in LOCKED_KINDS},
            "M_items": _items(groups["M"], False),
            "excluded_items": _items(groups["excluded"], False),
        },
        "warnings": list(dict.fromkeys(warnings)),
        "override_applied": None,
        "derived": True,
        "derived_from": {"case_id": profile.get("case_id"), "profile_version": profile.get("profile_version"),
                         "spending_report": spending_report is not None,
                         "income_gross_monthly": cf["income_gross"], "tax_monthly": cf["tax"]},
        "status": status,
        "unknown": list(dict.fromkeys(unknown)),
    }
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--report", type=Path, help="spending_report JSON(선택)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8")) if args.report else None
    step2 = build_step2(load_case(args.case), report)
    text = json.dumps(step2, ensure_ascii=False, indent=2)
    if args.out:
        if args.out.exists():
            raise SystemExit(f"{args.out}이 이미 있음. 덮어쓰지 않음")
        args.out.write_text(text, encoding="utf-8")
        print(f"→ {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
