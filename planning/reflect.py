"""정책 절감과 분배 결과를 반영한 step2 형식 JSON(반영본).

팀원 step2와 같은 최상위 키·순서를 유지하고 deltaM만 바꾼다. 후속 코드는 step2를 읽던 그대로 읽으면 된다.
- deltaM = spending_cut의 step2_link.deltaM_after[scenario]
- _meta.deltaM_detail의 a_monthly·fixed_after_monthly·variable_after_monthly를 반영값으로 바꾸고 policy_cut_monthly 추가
- _allocation: 반영 후 deltaM으로 부른 allocate() 요약
- _meta.policy_reflection: 전후 비교, 반영한 정책, 반영하지 않은 것

사용법:
    python -X utf8 -m planning.reflect --step2 planning/cases/kim_doyun_step2.json \
        --case customer/cases/kim_doyun.json --policy output/policy_kim_doyun_v2.json \
        --scenario certain --out output/step2_reflected_kim_doyun_certain_v1.json
    (--scenario both 이면 --out에 {scenario} 자리표시자)
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime
from pathlib import Path

from customer.profile import load_case

from .allocation import allocate
from .spending_cut import policy_from_db, spending_cut

SCENARIOS = {"certain": "입력상 충족 정책만", "potential": "확인 필요 정책 포함"}
GOAL_KEYS = ("goal", "name", "need", "existing", "gap", "years", "ratio", "ratio_by_need", "amount", "coverage",
             "monthly_needed", "active")
DETAIL_KEYS = ("deltaM", "a_monthly", "fixed_after_monthly", "variable_after_monthly")
NOTES = [
    "p·q·r·s·M 등 deltaM 외 최상위 값은 원본 그대로",
    "deltaM = 원본 deltaM + 정책 절감 연간 효과(월액 × min(지원 기간, 12)). 절감분은 전부 잉여현금 증가로 봄",
    "행동 절감 cut_monthly는 팀원 값 그대로. 정책 절감은 policy_cut_monthly로 따로 표시",
    "fixed_after·variable_after는 정책 월 절감만큼 줄인 값(변동은 variable_after 상한 적용)",
    "일시금·저축 정책·대출·금액 미상 정책과 상황형(to_check)은 반영하지 않음(not_reflected)",
    "선정 전 혜택이므로 certain도 확정 절감이 아님",
]


def _r(x):
    return None if x is None else round(x, 2)


def _other(scenario: str) -> str:
    return "potential" if scenario == "certain" else "certain"


def _check(step2: dict, cut: dict, scenario: str) -> None:
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario는 {list(SCENARIOS)} 중 하나여야 함: {scenario!r}")
    if "policy_reflection" in (step2.get("_meta") or {}):
        raise ValueError("이미 정책 절감이 반영된 step2임(_meta.policy_reflection 있음). 이중 반영 불가")
    link = cut.get("step2_link")
    if not link or link.get("skipped"):
        raise ValueError("cut에 step2_link가 없음. spending_cut(profile, policy, step2)로 만든 결과가 필요함")


def _applied(cut: dict, scenario: str) -> list[dict]:
    out = []
    for l in cut["lines"]:
        if not l[f"{scenario}_saving_won"]:
            continue
        name = l[f"{scenario}_policy"]
        p = next((x for x in l["policies"] if x["policy_name"] == name), {})
        out.append({"item": l["item"], "section": l["section"], "category": l["category"],
                    "policy_id": p.get("policy_id"), "policy_name": name, "verdict": p.get("verdict"),
                    "monthly_saving": l[f"{scenario}_saving"], "annual_saving": l[f"annual_{scenario}"],
                    "duration_months": p.get("duration_months"), "open_conditions": p.get("open_conditions", []),
                    "url": (p.get("apply") or {}).get("url")})
    return out


def _allocation_summary(allocation: dict, cut: dict) -> dict:
    return {
        "unit": allocation.get("unit"),
        "m_avail": allocation["m_avail"]["value"],
        "emergency": {"amount": allocation["emergency"]["amount"], "multiple": allocation["emergency"]["multiple"]},
        "distributable": allocation["distributable"],
        "goals": [{k: g.get(k) for k in GOAL_KEYS} for g in allocation["goals"]],
        "feasibility": copy.deepcopy(allocation["feasibility"]),
        "feasibility_before": copy.deepcopy((cut.get("allocation_effect") or {}).get("before")),
        "warnings": list(allocation.get("warnings", [])),
    }


def reflect(step2: dict, cut: dict, allocation: dict, scenario: str = "certain", *,
            sources: dict | None = None) -> dict:
    """step2 + spending_cut 결과 + (반영 후 deltaM으로 부른) allocate 결과 → 반영본 step2. 입력은 바꾸지 않는다."""
    _check(step2, cut, scenario)
    step2, cut, allocation = copy.deepcopy(step2), copy.deepcopy(cut), copy.deepcopy(allocation)
    link = cut["step2_link"]
    meta = step2.get("_meta") or {}
    detail = dict(meta.get("deltaM_detail") or {})

    before = {"deltaM": step2.get("deltaM"), "a_monthly": detail.get("a_monthly"),
              "fixed_after_monthly": detail.get("fixed_after_monthly"),
              "variable_after_monthly": detail.get("variable_after_monthly")}
    new_dm = link["deltaM_after"][scenario]["value"]
    after = {"deltaM": new_dm, "a_monthly": _r(new_dm / 12),
             "fixed_after_monthly": link["fixed_after_policy"][scenario],
             "variable_after_monthly": link["variable_after_policy"][scenario]}

    # deltaM_detail: 키 순서 유지, 세 값만 바꾸고 policy_cut_monthly 추가. messages는 새 리스트
    for k in ("a_monthly", "fixed_after_monthly", "variable_after_monthly"):
        detail[k] = after[k]
    detail["messages"] = list(detail.get("messages") or [])
    detail["policy_cut_monthly"] = dict(link["policy_saving_monthly"][scenario])
    income = detail.get("income_monthly")
    if income is not None:
        implied = round(income - after["fixed_after_monthly"] - after["variable_after_monthly"], 2)
        if abs(implied - after["a_monthly"]) > 0.01:
            detail["messages"].append(
                f"정책 반영: 지원 기간 12개월 미만 정책이 있어 a_monthly {after['a_monthly']}(= 반영 deltaM/12)가 "
                f"income−fixed_after−variable_after {implied}와 {round(after['a_monthly'] - implied, 2)} 다름")

    other = _other(scenario)
    other_dm = link["deltaM_after"][other]["value"]
    other_after = (cut.get("allocation_effect") or {}).get(f"after_{other}") or {}
    reflection = {
        "module": "reflect", "version": "v1", "scenario": scenario, "scenario_label": SCENARIOS[scenario],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sources": copy.deepcopy(sources) if sources is not None else {},
        "before": before, "after": after,
        "change": {k: None if before[k] is None else _r(after[k] - before[k]) for k in DETAIL_KEYS},
        "applied_policies": _applied(cut, scenario),
        "other_scenario": {"scenario": other, "deltaM": other_dm, "a_monthly": _r(other_dm / 12),
                           "shortfall_monthly": other_after.get("shortfall_monthly")},
        "not_reflected": {**cut["separate"]["counts"], "to_check_count": len(cut.get("to_check", []))},
        "notes": list(NOTES),
    }

    new_meta = {}
    for k, v in meta.items():
        new_meta[k] = detail if k == "deltaM_detail" else v
    if "deltaM_detail" not in new_meta:
        new_meta["deltaM_detail"] = detail
    new_meta["policy_reflection"] = reflection

    out = {}
    for k, v in step2.items():
        if k != "_meta":
            out[k] = new_dm if k == "deltaM" else v
    out["_allocation"] = _allocation_summary(allocation, cut)
    out["_meta"] = new_meta
    return out


def build(step2: dict, profile: dict, policy_result: dict, scenario: str = "certain", *,
          sources: dict | None = None, **allocate_kwargs) -> dict:
    """spending_cut → 반영 step2 → 반영 step2로 allocate() → reflect()로 합친다."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario는 {list(SCENARIOS)} 중 하나여야 함: {scenario!r}")
    cut = spending_cut(profile, policy_result, copy.deepcopy(step2), allocate_kwargs=allocate_kwargs or None)
    if not cut.get("step2_link") or cut["step2_link"].get("skipped"):
        raise ValueError("step2와 연결하지 못함: " + "; ".join(cut["warnings"]))
    s2 = copy.deepcopy(step2)
    s2["deltaM"] = cut["step2_link"]["deltaM_after"][scenario]["value"]
    allocation = allocate(s2, profile, **allocate_kwargs)
    sources = dict(sources or {})
    if allocate_kwargs:
        sources["allocate_options"] = allocate_kwargs
    return reflect(step2, cut, allocation, scenario, sources=sources)


def _out_paths(out: Path, scenario: str) -> dict[str, Path]:
    if scenario == "both":
        if "{scenario}" not in str(out):
            raise SystemExit("--scenario both에는 --out에 {scenario} 자리표시자가 필요함")
        return {s: Path(str(out).replace("{scenario}", s)) for s in SCENARIOS}
    return {scenario: out}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step2", required=True, type=Path)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--policy", type=Path, help="없으면 DB에서 판정(읽기 전용)")
    parser.add_argument("--scenario", default="certain", choices=[*SCENARIOS, "both"])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--force", action="store_true", help="--out 파일이 있어도 덮어씀")
    parser.add_argument("--db", default=Path("data/customer_profiles.db"), type=Path)
    parser.add_argument("--policy-db", default=Path("data/youth_policy_enriched.db"), type=Path)
    parser.add_argument("--m-includes-locked", action="store_true")
    parser.add_argument("--retire-age", type=float)
    parser.add_argument("--emergency-multiple", type=float)
    args = parser.parse_args(argv)

    outs = _out_paths(args.out, args.scenario)
    existing = [str(p) for p in outs.values() if p.exists()]
    if existing and not args.force:
        raise SystemExit(f"이미 있음(덮어쓰지 않음, --force로 덮어씀): {', '.join(existing)}")

    step2 = json.loads(args.step2.read_text(encoding="utf-8"))
    profile = load_case(args.case)
    if args.policy:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
    else:
        try:
            policy = policy_from_db(profile["case_id"], profile["profile_version"], args.db, args.policy_db)
        except Exception as e:
            raise SystemExit(f"정책 판정을 DB에서 만들지 못함({e}). --policy 파일을 주세요")
    kwargs = {k: v for k, v in (("retire_age", args.retire_age), ("emergency_multiple", args.emergency_multiple))
              if v is not None}
    if args.m_includes_locked:
        kwargs["m_includes_locked"] = True
    sources = {"step2": args.step2.as_posix(), "case": args.case.as_posix(),
               "policy": args.policy.as_posix() if args.policy else "DB 판정(읽기 전용)",
               "policy_db_as_of": policy.get("search_scope", {}).get("policy_db_as_of"),
               "case_id": profile["case_id"], "profile_version": profile["profile_version"]}
    for scenario, path in outs.items():
        result = build(step2, profile, policy, scenario, sources=sources, **kwargs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        r = result["_meta"]["policy_reflection"]
        print(f"→ {path} [{scenario}] deltaM {r['before']['deltaM']} → {r['after']['deltaM']}, "
              f"shortfall {result['_allocation']['feasibility']['shortfall_monthly']}")


if __name__ == "__main__":
    main()
