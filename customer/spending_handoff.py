"""정책 판정 결과(module_result 'policy') → 소비습관 개선 에이전트용 요약본.

정책 결과는 "어떤 정책에 자격이 되는가"이고, 소비 에이전트에 필요한 것은 "어느 지출 항목이 얼마나 줄어드는가"다.
이 모듈은 고객 현금흐름의 지출 항목마다 줄여 줄 수 있는 정책을 붙이고, 월 절감 예상액·확실성·본인 부담·할 일만 남긴다.

규칙:
- '입력상 충족' 정책만 절감 시나리오에 넣고, '확인 필요'는 열린 조건과 함께 따로 둔다.
- 금액을 모르면 0이 아니라 null + 공고 확인. 일시금은 월 절감과 섞지 않는다.
- 같은 지출 항목에 정책이 여럿이면 합치지 않고 가장 큰 것 하나만 시나리오 합계에 쓴다(중복 적용 여부 미확인).
- 대출·보증료 같은 부채 수단은 소비 요약에서 빼고 건수만 남긴다(자금 배분·부채 모듈 몫).

사용법:
    python -m customer.spending_handoff kim_doyun [--version v1] [--db data/customer_profiles.db] [--out output/]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

# (지출 범주, 정책을 알아보는 패턴, 고객 현금흐름 항목을 알아보는 패턴)
CATEGORIES = [
    ("주거비", r"월세|임차|임대료|주거비|주거급여|중개수수료|청년하우스|쉐어하우스", r"임대료|월세"),
    ("교통비", r"교통|K-?패스|동행\s*카드|대중교통", r"교통"),
    ("문화생활비", r"문화|컬처|관람|공연|전시|예술\s*패스|체육|스포츠", r"문화"),
    ("자기개발비", r"자격|응시료|평생교육|수강|도서|역량\s*강화|어학|스펙|교육\s*이용권", r"자기개발"),
    ("통신비", r"통신|요금\s*할인", r"통신"),
    ("의료비", r"의료|치료|건강|검진|심리|마음|정신", r"의료"),
]
SAVINGS_TYPES = {"매칭저축", "저축상품"}
DEBT_TYPES = {"대출", "보증료지원"}
# 기타 서비스 목록에서는 소비자에게 직접 돈·할인을 주는 유형만 지출 항목에 붙인다
CONSUMER_TYPES = {"할인·환급", "바우처·현물", "현금지원", "월세·주거비"}
# 이름에 지출 키워드가 있어도 인력양성·연구·일자리 사업이면 지출 절감이 아니다
_NOT_SPENDING_RE = re.compile(r"양성|인턴|연구|R&D|일자리|채용|사업단|시범|연수단원|인재|전문가|육성|창업")
# 지출 항목 세부 내역에서 정책이 실제로 적용되는 부분(만원)
_DETAIL_LABEL = {"교통비": "대중교통", "의료비": "의료"}
# 지출 항목 일부에만 적용될 수 있는 범주. 비율 혜택은 [0, 상한] 범위로 둔다
_PARTIAL_CATEGORIES = {"자기개발비", "문화생활비"}
_HOUSING_MOVE_RE = re.compile(r"입주|임대주택|하우스|공공임대")


def _won(v):
    return int(round(v)) if v is not None else None


def _detail_won(detail: str | None, label: str) -> int | None:
    """'대중교통 8 + 택시 2(만원)'처럼 세부 내역에서 해당 부분 금액(만원)을 꺼낸다."""
    m = re.search(rf"{label}\s*([\d.]+)", detail or "")
    return int(float(m.group(1)) * 10_000) if m else None


def estimate_effect(benefit: dict, expense: dict | None, policy_name: str = "") -> dict:
    """정책 혜택이 해당 지출을 월 얼마 줄이는지. 근거와 가정을 같이 돌려준다."""
    monthly_expense = expense["monthly_won"] if expense else None
    category = expense.get("category") if expense else None
    base, base_note = monthly_expense, None
    label = _DETAIL_LABEL.get(category)
    if label:
        part = _detail_won(expense.get("detail"), label)
        if part is not None:
            base, base_note = part, f"{expense['item']} 중 {label}분 월 {part:,}원에만 적용"
    assumptions = [base_note] if base_note else []

    if category == "주거비" and _HOUSING_MOVE_RE.search(policy_name):
        return {"kind": "주거이전", "basis": "입주형 주거 지원. 이사해야 효과가 생기므로 월 절감으로 계산하지 않음",
                "assumptions": assumptions}
    if expense is not None and monthly_expense is None and (benefit.get("discount_pct") or benefit.get("monthly_won")
                                                             or benefit.get("discount_won")):
        return {"kind": "지출미확인", "basis": "이 지출의 월 금액이 없어 계산하지 못함", "assumptions": assumptions}
    if benefit.get("discount_pct") and base is not None:
        saving = base * benefit["discount_pct"] / 100
        if benefit.get("monthly_won"):
            saving = min(saving, benefit["monthly_won"])
        low = 0 if category in _PARTIAL_CATEGORIES else saving
        if low == 0:
            assumptions.append(f"{expense['item']} 중 정책 대상 지출 비중 미확인. 상한값")
        return {"kind": "비율", "monthly_saving_won": [_won(low), _won(saving)],
                "basis": f"월 {base:,}원 × {benefit['discount_pct']:g}%", "assumptions": assumptions}
    if benefit.get("monthly_won"):
        saving = min(benefit["monthly_won"], monthly_expense) if monthly_expense else benefit["monthly_won"]
        return {"kind": "월정액", "monthly_saving_won": [_won(saving), _won(saving)],
                "months": benefit.get("duration_months"),
                "basis": f"정책 월 {benefit['monthly_won']:,}원" + (f", 지출 {monthly_expense:,}원 한도" if monthly_expense else ""),
                "assumptions": assumptions}
    if benefit.get("discount_won"):
        return {"kind": "정액할인", "monthly_saving_won": [benefit["discount_won"], benefit["discount_won"]],
                "basis": f"1회 {benefit['discount_won']:,}원 할인",
                "assumptions": assumptions + ["월 1회 구매·이용 가정"]}
    one_off = benefit.get("total_won")
    rng = benefit.get("range_won")
    if one_off or rng:
        lo, hi = (one_off, one_off) if one_off else tuple(rng)
        return {"kind": "일시금", "one_off_won": [lo, hi],
                "monthly_equivalent_won": [_won(lo / 12), _won(hi / 12)],
                "basis": "총액 ÷ 12개월(연 1회 수령 가정)", "assumptions": assumptions + ["월 절감과 합산하지 않음"]}
    return {"kind": "금액미상", "basis": "공고 확인 필요", "assumptions": assumptions}


def _open_conditions(finding: dict) -> list[str]:
    return [f"{c['condition']}: {c['note'] or c['policy_value']}"
            for c in finding["conditions"] if c["result"] == "미확인"]


def _offset(finding: dict, expense: dict | None) -> dict:
    b = finding["benefit"]
    return {
        "policy_id": finding["policy_id"],
        "policy_name": finding["policy_name"],
        "benefit_summary": b["summary"],
        "certainty": "시나리오 사용 가능" if finding["verdict"] == "입력상 충족" else "확인 필요",
        "open_conditions": _open_conditions(finding),
        "effect": estimate_effect(b, expense, finding["policy_name"]),
        "own_cost": "지정 사용처·조건에서 써야 혜택" if b["type"] in ("바우처·현물", "할인·환급") else None,
        "action": {"deadline": finding["apply"]["apply_end_date"], "status": finding["apply"]["recruit_status"],
                   "url": finding["apply"]["url"]},
    }


def _category(finding: dict, categories=CATEGORIES) -> str | None:
    text = f"{finding['policy_name']} {finding['benefit']['summary']}"
    if finding["benefit"]["type"] == "월세·주거비":
        return "주거비"
    for name, pattern, _ in categories:
        if re.search(pattern, text):
            return name
    return None


def build(policy_result: dict, cashflow: list[dict], liabilities: list[dict],
          categories=CATEGORIES, other_filter=None) -> dict:
    """categories: (범주, 정책 패턴, 지출 항목 패턴) 목록. other_filter: 기타 서비스 중 지출에 붙일 것을 고르는 함수.
    둘 다 생략하면 기본 6개 범주와 CONSUMER_TYPES 기준(planning.spending_cut이 확장해서 부른다)."""
    keep_other = other_filter or (lambda f: f["benefit"]["type"] in CONSUMER_TYPES)
    expense_patterns = {name: exp for name, _, exp in categories}
    lines = []
    for item in cashflow:
        if item["section"] in ("저축", "세금·사회보험"):
            continue
        category = next((n for n, p in expense_patterns.items()
                         if re.search(p, item["item"]) or (n == "의료비" and re.search(p, item.get("detail") or ""))),
                        None)
        lines.append({"item": item["item"], "section": item["section"], "category": category,
                      "monthly_won": item["monthly_won"], "purpose": item["purpose"], "necessity": item["necessity"],
                      "detail": item.get("detail"), "policy_offsets": []})
    by_category = {}
    for line in lines:
        # 0원으로 적힌 항목에는 절감을 붙이지 않는다. 금액이 빈칸인 항목은 붙여 두고 금액을 묻는다
        if line["category"] and (line["monthly_won"] is None or line["monthly_won"] > 0):
            by_category.setdefault(line["category"], line)

    savings, one_off_income, unmapped, targeted, debt_count = [], [], [], [], 0
    has_loan = any((l["balance_won"] or 0) > 0 for l in liabilities)
    main_ids = {f["policy_id"] for f in policy_result["findings"]}
    findings = policy_result["findings"] + [
        f for f in policy_result.get("other_findings", []) if keep_other(f)]
    for f in findings:
        btype = f["benefit"]["type"]
        if _NOT_SPENDING_RE.search(f["policy_name"]):
            continue
        # 정책명으로만 드러난 특정 집단 대상(예술인·취약계층 등)이 미확인이면 소비 요약에서 빼고 이름만 남긴다
        if any(c["condition"] == "대상 집단(정책명)" and c["result"] == "미확인" for c in f["conditions"]):
            targeted.append(f["policy_name"])
            continue
        if btype in SAVINGS_TYPES:
            b = f["benefit"]
            savings.append({"policy_id": f["policy_id"], "policy_name": f["policy_name"], "type": btype,
                            "certainty": "시나리오 사용 가능" if f["verdict"] == "입력상 충족" else "확인 필요",
                            "own_saving_monthly_won": b["own_saving_monthly_won"], "match_ratio": b["match_ratio"],
                            "benefit_summary": b["summary"], "open_conditions": _open_conditions(f),
                            "note": "본인 저축이 필요하므로 지출 예산을 줄여 재원을 마련해야 함",
                            "action": {"deadline": f["apply"]["apply_end_date"], "url": f["apply"]["url"]}})
            continue
        if btype in DEBT_TYPES:
            debt_count += 1
            continue
        if btype == "이자지원":
            if has_loan and "대출 원리금" in {l["item"] for l in lines}:
                line = next(l for l in lines if l["item"] == "대출 원리금")
                line["policy_offsets"].append(_offset(f, line))
            else:
                debt_count += 1
            continue
        category = _category(f, categories)
        if category and category in by_category:
            line = by_category[category]
            line["policy_offsets"].append(_offset(f, {**line, "category": category}))
        elif f["policy_id"] in main_ids and btype in ("현금지원", "바우처·현물") and \
                (f["benefit"]["total_won"] or f["benefit"]["range_won"] or f["benefit"]["monthly_won"]):
            one_off_income.append({"policy_id": f["policy_id"], "policy_name": f["policy_name"],
                                   "certainty": "시나리오 사용 가능" if f["verdict"] == "입력상 충족" else "확인 필요",
                                   "effect": estimate_effect(f["benefit"], None, f["policy_name"]),
                                   "open_conditions": _open_conditions(f),
                                   "action": {"deadline": f["apply"]["apply_end_date"], "url": f["apply"]["url"]}})
        elif f["policy_id"] in main_ids:
            unmapped.append(f["policy_name"])

    scenario_total = 0
    potential_total = 0
    for line in lines:
        def best(offsets):
            vals = [o["effect"]["monthly_saving_won"][0] for o in offsets if o["effect"].get("monthly_saving_won")]
            return max(vals) if vals else 0
        line["policy_offsets"].sort(key=lambda o: (o["certainty"] != "시나리오 사용 가능",
                                                   -(o["effect"].get("monthly_saving_won") or [0])[0]))
        sure = best([o for o in line["policy_offsets"] if o["certainty"] == "시나리오 사용 가능"])
        maybe = best(line["policy_offsets"])
        line["scenario_monthly_saving_won"] = sure
        line["potential_monthly_saving_won"] = maybe
        scenario_total += sure
        potential_total += maybe
        line.pop("detail", None) if not line["policy_offsets"] else None

    questions = [q for q in policy_result.get("questions", [])]
    return {
        "module": "policy_to_spending",
        "case_id": policy_result["case_id"],
        "profile_version": policy_result["profile_version"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"module": "policy", "generated_at": policy_result["generated_at"],
                   "policy_db_as_of": policy_result["search_scope"]["policy_db_as_of"]},
        "totals": {
            "scenario_monthly_saving_won": scenario_total,
            "potential_monthly_saving_won": potential_total,
            "note": "항목마다 가장 큰 정책 하나만 합산. scenario는 '입력상 충족'만, potential은 '확인 필요' 포함",
        },
        "expense_lines": lines,
        "savings_programs": savings,
        "one_off_income": one_off_income,
        "excluded": {"debt_instruments": debt_count, "unmapped_financial": unmapped,
                     "targeted_group_unverified": targeted,
                     "other_services": len(policy_result.get("other_findings", []))},
        "questions": questions,
        "rules": [
            "선정 전 혜택은 확정 절감이 아님. 시나리오로만 사용",
            "일시금(one_off)은 월 절감 합계에 넣지 않음",
            "같은 지출 항목의 여러 정책은 중복 적용 여부를 공고에서 확인",
            "고객이 필수라고 한 지출은 정책이 있어도 줄이라고 권하지 않음",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--version")
    parser.add_argument("--db", default=Path("data/customer_profiles.db"), type=Path)
    parser.add_argument("--out", default=Path("output"), type=Path)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    version = args.version or conn.execute(
        "SELECT profile_version FROM module_results WHERE case_id=? AND module='policy' ORDER BY created_at DESC LIMIT 1",
        (args.case_id,)).fetchone()[0]
    key = (args.case_id, version)
    row = conn.execute("SELECT result_json FROM module_results WHERE case_id=? AND profile_version=? AND module='policy'",
                       key).fetchone()
    if row is None:
        raise SystemExit(f"{key}의 정책 결과가 없습니다. 먼저 python -m customer.policy_match {args.case_id}")
    cashflow = [dict(r) for r in conn.execute(
        "SELECT section, item, monthly_won, purpose, necessity, detail FROM cashflow_items "
        "WHERE case_id=? AND profile_version=?", key)]
    liabilities = [dict(r) for r in conn.execute(
        "SELECT kind, balance_won, rate_pct FROM liabilities WHERE case_id=? AND profile_version=?", key)]
    result = build(json.loads(row["result_json"]), cashflow, liabilities)

    conn.execute("INSERT OR REPLACE INTO module_results VALUES (?,?,?,?,?,?)",
                 (*key, result["module"], "completed", json.dumps(result, ensure_ascii=False), result["generated_at"]))
    conn.commit()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"spending_handoff_{args.case_id}_{version}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"file": str(path), "bytes": path.stat().st_size, **result["totals"],
                      "excluded": result["excluded"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
