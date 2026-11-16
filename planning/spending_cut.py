"""정책 매칭 결과로 줄일 수 있는 지출(만원/월)과 그 효과.

정책을 지출 항목에 붙이는 일은 customer.spending_handoff.build()가 이미 한다. 여기서는
- 범주를 넓혀(식비) build()를 다시 부르고,
- 항목별 절감에 상한(지출액·적용 부분)을 두고 certain(입력상 충족)·potential(확인 필요 포함)로 나누고,
- step2의 변동·고정지출, 연 잉여현금(deltaM)과 이어 붙인 뒤,
- planning.allocation.allocate()로 달성 가능성이 어떻게 바뀌는지 전후를 남긴다.

사용법:
    python -X utf8 -m planning.spending_cut --case customer/cases/kim_doyun.json \
        --policy output/policy_kim_doyun_v2.json --step2 planning/cases/kim_doyun_step2.json \
        --out output/spending_cut_kim_doyun_v1.json
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
from pathlib import Path

from customer.profile import load_case
from customer.spending_handoff import _NOT_SPENDING_RE, CATEGORIES, CONSUMER_TYPES, _category, _detail_won, build

from .allocation import allocate

# ── 확장 범주 ─────────────────────────────────────────────────
# 2026-11-12 조사: 고객 6명의 판정 결과에서 금액을 읽을 수 있는 식비 정책은 '직장인 든든한 한끼'(월 4만원, 기타 서비스),
# 상황형 '직장인 든든한 점심밥'(20%, 월 4만원 한도). 지역화폐·상품권 할인형은 0건이라 소비 전반 범주는 두지 않는다.
FOOD_POLICY_RE = r"한끼|점심밥|아침밥|식권|외식비|(?<!예)식비|농식품\s*바우처|급식|도시락|반찬"
EXT_CATEGORIES = CATEGORIES + [("식비", FOOD_POLICY_RE, r"생활비|식비")]
# 기타 서비스 중 'benefit.type=기타'라도 식비 정책이면 지출에 붙인다
_FOOD_RE = re.compile(FOOD_POLICY_RE)


def ext_other_filter(f: dict) -> bool:
    if f["benefit"]["type"] in CONSUMER_TYPES:
        return True
    return f["benefit"]["type"] == "기타" and bool(_FOOD_RE.search(f"{f['policy_name']} {f['benefit']['summary']}"))


# 지출 항목 세부 내역에서 정책이 실제로 적용되는 부분. 교통·의료는 handoff와 같고 식비는 직장 점심.
APPLICABLE_PART = {"교통비": "대중교통", "의료비": "의료", "식비": r"직장\s*식사"}
SECTIONS = ("변동지출", "고정지출", "보험")
TO_CHECK_MAX = 5

RULES = [
    "국가 지원금은 가계 지출 방어에 최우선 투입: 정책 절감분은 전부 잉여현금(deltaM) 증가로 본다",
    "certain은 '입력상 충족'만, potential은 '확인 필요' 포함. 선정 전 혜택이라 확정 절감이 아님",
    "한 항목의 절감은 그 지출액(세부 내역에서 적용 부분이 드러나면 그 부분)을 넘지 않는다",
    "같은 항목에 정책이 여럿이면 가장 큰 것 하나만 합산(중복 수혜 미확인). 비율 혜택은 handoff 하한값 사용",
    "금액을 모르는 정책은 null('공고 확인')로 두고 합계에서 뺀다",
    "일시금·저축 정책·대출은 월 절감과 섞지 않고 separate에 둔다",
    "연간 효과 = 월액 × min(지원 기간, 12). 기간 정보가 없으면 12개월",
    "step2의 cut_monthly(소비습관 행동 절감)와 정책 절감은 별개. 변동지출 정책 절감은 variable_after_monthly를 넘지 않는다",
    "상황형(situational) 정책은 자격을 알 수 없어 절감에 넣지 않고 to_check에만 둔다",
]


def _man(won):
    return None if won is None else round(won / 10_000, 2)


def _part_won(line: dict) -> int | None:
    label = APPLICABLE_PART.get(line["category"])
    return _detail_won(line.get("detail"), label) if label else None


# ── 항목별 절감 ───────────────────────────────────────────────
def _policy_row(o: dict) -> dict:
    eff = o["effect"]
    monthly = (eff.get("monthly_saving_won") or [None])[0] if eff.get("monthly_saving_won") else None
    note = None
    if monthly is None:
        note = "일시금: 월 절감과 별도(separate)" if eff["kind"] == "일시금" else "공고 확인"
    return {
        "policy_id": o["policy_id"], "policy_name": o["policy_name"],
        "verdict": "입력상 충족" if o["certainty"] == "시나리오 사용 가능" else "확인 필요",
        "benefit_summary": o.get("benefit_summary"),
        "monthly_saving_won": monthly,
        "monthly_saving_upper_won": eff["monthly_saving_won"][1] if eff.get("monthly_saving_won") else None,
        "duration_months": eff.get("months"),
        "effect_kind": eff["kind"], "effect_basis": eff.get("basis"), "assumptions": eff.get("assumptions", []),
        "open_conditions": o.get("open_conditions", []), "apply": o.get("action"), "note": note,
    }


def _best(policies: list[dict], cap: int | None):
    """금액이 있는 정책 중 가장 큰 것 → (상한 적용 월 절감, 정책). 없으면 (0, None)."""
    known = [p for p in policies if p["monthly_saving_won"] is not None]
    if not known:
        return 0, None
    top = max(known, key=lambda p: p["monthly_saving_won"])
    value = top["monthly_saving_won"] if cap is None else min(top["monthly_saving_won"], cap)
    return value, top


def _annual(monthly: int, policy: dict | None) -> int:
    months = (policy or {}).get("duration_months")
    return monthly * (min(months, 12) if months else 12)


def line_saving(line: dict) -> dict:
    before = line["monthly_won"]
    part = _part_won(line)
    cap = part if part is not None else before
    policies = [_policy_row(o) for o in line["policy_offsets"]]
    sure, sure_p = _best([p for p in policies if p["verdict"] == "입력상 충족"], cap)
    maybe, maybe_p = _best(policies, cap)
    notes = []
    if part is not None:
        notes.append(f"세부 내역 중 적용 부분 월 {part:,}원을 상한으로 씀")
    if before is None and policies:
        notes.append("지출액 미확인: 절감을 계산하지 못함")
    if any(p["monthly_saving_won"] is None for p in policies):
        notes.append("금액 모르는 정책은 합계에서 제외(공고 확인)")
    if maybe_p and maybe < maybe_p["monthly_saving_won"]:
        notes.append(f"정책 월 {maybe_p['monthly_saving_won']:,}원을 상한 {cap:,}원으로 줄임")
    out = {"item": line["item"], "section": line["section"], "category": line["category"],
           "before_won": before, "applicable_part_won": part,
           "certain_saving_won": sure, "potential_saving_won": maybe,
           "after_certain_won": None if before is None else before - sure,
           "after_potential_won": None if before is None else before - maybe,
           "annual_certain_won": _annual(sure, sure_p), "annual_potential_won": _annual(maybe, maybe_p),
           "certain_policy": sure_p["policy_name"] if sure_p else None,
           "potential_policy": maybe_p["policy_name"] if maybe_p else None,
           "policies": policies, "notes": notes}
    for k in ("before", "certain_saving", "potential_saving", "after_certain", "after_potential",
              "annual_certain", "annual_potential"):
        out[k] = _man(out[f"{k}_won"])
    return out


def _totals(lines: list[dict]) -> dict:
    out = {}
    for side, secs in (("variable", ("변동지출",)), ("fixed", ("고정지출", "보험"))):
        rows = [l for l in lines if l["section"] in secs]
        for kind in ("certain", "potential"):
            m = sum(l[f"{kind}_saving_won"] for l in rows)
            a = sum(l[f"annual_{kind}_won"] for l in rows)
            out[f"{side}_{kind}"] = {"monthly_won": m, "annual_won": a, "monthly": _man(m), "annual": _man(a)}
    out["note"] = "fixed는 고정지출+보험. 항목마다 가장 큰 정책 하나만 합산"
    return out


# ── 확인해 볼 정책 ────────────────────────────────────────────
def to_check(policy_result: dict, lines: list[dict]) -> list[dict]:
    # 상황형도 소비자에게 직접 돈·할인을 주는 유형(또는 식비 정책)만 본다
    situational = [f for f in policy_result.get("situational_findings", [])
                   if not _NOT_SPENDING_RE.search(f["policy_name"]) and ext_other_filter(f)]
    out = []
    for l in lines:
        if l["section"] != "변동지출" or not l["category"]:
            continue
        cands = [{"policy_name": p["policy_name"], "kind": "확인 필요", "verdict": p["verdict"],
                  "benefit_summary": p["benefit_summary"], "monthly_saving_won": p["monthly_saving_won"],
                  "open_conditions": p["open_conditions"], "url": (p["apply"] or {}).get("url")}
                 for p in l["policies"] if p["verdict"] == "확인 필요"]
        for f in situational:
            if _category(f, EXT_CATEGORIES) == l["category"]:
                b = f["benefit"]
                cands.append({"policy_name": f["policy_name"], "kind": "상황형(자격 미확인)", "verdict": f["verdict"],
                              "benefit_summary": b["summary"], "monthly_saving_won": None,
                              "open_conditions": [c["condition"] for c in f["conditions"] if c["result"] == "미확인"],
                              "url": f["apply"]["url"]})
        # 금액이 드러난 정책을 앞에(확인 필요의 월 절감 → 상황형 중 금액 요약이 있는 것 → 금액 정보 없음)
        cands.sort(key=lambda c: (c["monthly_saving_won"] is None, "금액 정보 없음" in (c["benefit_summary"] or "")))
        if cands:
            out.append({"item": l["item"], "category": l["category"], "policies": cands[:TO_CHECK_MAX],
                        "more": max(len(cands) - TO_CHECK_MAX, 0)})
    return out


# ── step2·분배 연결 ───────────────────────────────────────────
def _diff_warn(label, profile_v, step2_v, warnings):
    if step2_v and abs(profile_v - step2_v) / step2_v > 0.05:
        warnings.append(f"{label}: 프로필 {profile_v:,.1f}만원 vs step2 {step2_v:,.1f}만원 "
                        f"({(profile_v - step2_v) / step2_v:+.1%}) → step2 값을 기준으로 씀")
    return {"profile": round(profile_v, 2), "step2": step2_v,
            "diff_pct": None if not step2_v else round((profile_v - step2_v) / step2_v * 100, 2)}


def step2_link(step2: dict, lines: list[dict], totals: dict, warnings: list) -> dict:
    d = step2.get("_meta", {}).get("deltaM_detail", {})
    var_after, cut, fixed_after = d.get("variable_after_monthly"), d.get("cut_monthly"), d.get("fixed_after_monthly")
    deltaM = step2.get("deltaM")
    if None in (var_after, cut, fixed_after, deltaM):
        warnings.append("step2에 variable_after_monthly·cut_monthly·fixed_after_monthly·deltaM 중 빈 값 → 연결 생략")
        return {"skipped": True}
    prof_var = sum(l["before_won"] or 0 for l in lines if l["section"] == "변동지출") / 10_000
    prof_fixed = sum(l["before_won"] or 0 for l in lines if l["section"] in ("고정지출", "보험")) / 10_000
    out = {
        "basis": "step2 값 기준. 프로필 합계는 비교용",
        "compare_variable": _diff_warn("변동지출(step2는 variable_after+cut)", prof_var, round(var_after + cut, 2), warnings),
        "compare_fixed": _diff_warn("고정지출+보험(step2는 fixed_after)", prof_fixed, fixed_after, warnings),
        "behavior_cut_monthly": cut,
        "variable_after_monthly": var_after, "fixed_after_monthly": fixed_after, "deltaM": deltaM,
        "policy_saving_monthly": {}, "variable_after_policy": {}, "fixed_after_policy": {},
        "annual_effect": {}, "deltaM_after": {},
    }
    for kind in ("certain", "potential"):
        parts = {}
        for side, limit in (("variable", var_after), ("fixed", fixed_after)):
            raw = totals[f"{side}_{kind}"]["monthly"]
            capped = min(raw, limit)
            if capped < raw:
                warnings.append(f"{side} 정책 절감({kind}) {raw}만원이 step2 {limit}만원을 넘어 상한 적용")
            scale = capped / raw if raw else 0
            parts[side] = {"monthly": round(capped, 2), "raw_monthly": raw,
                           "annual": round(totals[f"{side}_{kind}"]["annual"] * scale, 2)}
        out["policy_saving_monthly"][kind] = {s: parts[s]["monthly"] for s in parts}
        out["variable_after_policy"][kind] = round(var_after - parts["variable"]["monthly"], 2)
        out["fixed_after_policy"][kind] = round(fixed_after - parts["fixed"]["monthly"], 2)
        out["annual_effect"][kind] = {s: parts[s]["annual"] for s in parts}
        out["deltaM_after"][kind] = {"value": round(deltaM + parts["variable"]["annual"] + parts["fixed"]["annual"], 2),
                                     "from_variable": parts["variable"]["annual"], "from_fixed": parts["fixed"]["annual"]}
    return out


def allocation_effect(step2: dict, profile: dict, link: dict, allocate_kwargs: dict | None = None) -> dict:
    def feas(s):
        f = allocate(s, profile, **(allocate_kwargs or {}))["feasibility"]
        return {k: f[k] for k in ("monthly_needed_total", "monthly_surplus", "shortfall_monthly", "feasible")}
    out = {"before": feas(copy.deepcopy(step2)), "note": "allocate()를 deltaM만 바꿔 다시 부름. 분배 비율·금액은 그대로"}
    for kind in ("certain", "potential"):
        s = copy.deepcopy(step2)
        s["deltaM"] = link["deltaM_after"][kind]["value"]
        after = feas(s)
        after["shortfall_change"] = None if after["shortfall_monthly"] is None else round(
            after["shortfall_monthly"] - out["before"]["shortfall_monthly"], 2)
        out[f"after_{kind}"] = after
    return out


# ── 본체 ──────────────────────────────────────────────────────
def spending_cut(profile: dict, policy_result: dict, step2: dict | None = None, *,
                 allocate_kwargs: dict | None = None) -> dict:
    """allocate_kwargs: 달성 가능성 전후를 계산할 때 allocate()에 넘길 인자(예: goal_basis). 없으면 기본."""
    warnings: list[str] = []
    handoff = build(policy_result, profile["cashflow_items"], profile["liabilities"],
                    categories=EXT_CATEGORIES, other_filter=ext_other_filter)
    raw = [l for l in handoff["expense_lines"] if l["section"] in SECTIONS]
    raw.sort(key=lambda l: SECTIONS.index(l["section"]))
    lines = [line_saving(l) for l in raw]
    for l in lines:
        if l["before_won"] is None:
            warnings.append(f"'{l['item']}' 지출액 미확인")
    totals = _totals(lines)
    on_lines_one_off = [{"item": l["item"], **p} for l in lines for p in l["policies"] if p["effect_kind"] == "일시금"]
    result = {
        "module": "spending_cut", "version": "v1", "unit": "만원(·_won은 원)",
        "inputs": {"case_id": profile["case_id"], "profile_version": profile["profile_version"],
                   "policy_generated_at": policy_result.get("generated_at"),
                   "policy_db_as_of": policy_result.get("search_scope", {}).get("policy_db_as_of"),
                   "categories": [c for c, _, _ in EXT_CATEGORIES], "step2": step2 is not None},
        "lines": lines,
        "totals": totals,
        "step2_link": None, "allocation_effect": None,
        "separate": {
            "one_off_income": handoff["one_off_income"], "one_off_on_lines": on_lines_one_off,
            "savings_programs": handoff["savings_programs"],
            "counts": {"one_off": len(handoff["one_off_income"]) + len(on_lines_one_off),
                       "savings": len(handoff["savings_programs"]),
                       "debt": handoff["excluded"]["debt_instruments"]},
            "excluded": handoff["excluded"],
        },
        "to_check": to_check(policy_result, lines),
        "warnings": warnings,
        "rules": RULES,
    }
    if step2 is not None:
        link = step2_link(step2, lines, totals, warnings)
        result["step2_link"] = link
        if not link.get("skipped"):
            result["allocation_effect"] = allocation_effect(step2, profile, link, allocate_kwargs)
    return result


def policy_from_db(case_id: str, version: str, customer_db: Path, policy_db: Path) -> dict:
    """정책 결과 파일이 없을 때: 두 DB를 읽기 전용으로 열어 customer.policy_match.match()를 부른다(저장 안 함)."""
    from customer.policy_match import load_customer, match
    ro = lambda p: sqlite3.connect(f"{Path(p).resolve().as_uri()}?mode=ro", uri=True)
    with ro(customer_db) as cconn, ro(policy_db) as pconn:
        return match(pconn, load_customer(cconn, case_id, version))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--policy", type=Path, help="output/policy_<case>_<ver>.json. 없으면 DB에서 판정(읽기 전용)")
    parser.add_argument("--step2", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--db", default=Path("data/customer_profiles.db"), type=Path)
    parser.add_argument("--policy-db", default=Path("data/youth_policy_enriched.db"), type=Path)
    parser.add_argument("--force", action="store_true", help="--out 파일이 있어도 덮어씀")
    args = parser.parse_args()

    profile = load_case(args.case)
    if args.policy:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
    else:
        try:
            policy = policy_from_db(profile["case_id"], profile["profile_version"], args.db, args.policy_db)
        except Exception as e:  # DB가 없거나 해당 버전이 저장돼 있지 않으면 파일이 필요하다
            raise SystemExit(f"정책 판정을 DB에서 만들지 못함({e}). --policy 파일을 주세요")
    step2 = json.loads(args.step2.read_text(encoding="utf-8")) if args.step2 else None
    result = spending_cut(profile, policy, step2)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        if args.out.exists() and not args.force:
            raise SystemExit(f"{args.out}이 이미 있음. 덮어쓰지 않음")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"→ {args.out}")
    for l in result["lines"]:
        print(f"[{l['section']}] {l['item']}: before {l['before']} certain {l['certain_saving']} "
              f"potential {l['potential_saving']} ({l['potential_policy']})")
    print(json.dumps({k: v for k, v in result["totals"].items() if k != "note"}, ensure_ascii=False))
    if result["step2_link"]:
        print(json.dumps({k: result["step2_link"].get(k) for k in ("deltaM", "deltaM_after", "variable_after_policy")},
                         ensure_ascii=False))
        print(json.dumps(result["allocation_effect"], ensure_ascii=False))
    for w in result["warnings"]:
        print("  [경고]", w)


if __name__ == "__main__":
    main()
