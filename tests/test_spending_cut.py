"""planning.spending_cut 시험. DB·웹 없이 고정 입력만 쓴다."""
import copy
import json

import pytest

from customer.spending_handoff import CATEGORIES, build
from planning.spending_cut import EXT_CATEGORIES, ext_other_filter, spending_cut


def finding(name, verdict="입력상 충족", btype="할인·환급", summary="요약", open_cond=False, **benefit):
    b = {"type": btype, "summary": summary, "monthly_won": None, "duration_months": None, "total_won": None,
         "range_won": None, "discount_pct": None, "discount_won": None, "own_saving_monthly_won": None,
         "match_ratio": None}
    b.update(benefit)
    conds = [{"condition": "특화 대상", "policy_value": "중소기업", "result": "미확인", "note": "재직 확인"}] if open_cond else []
    return {"policy_id": f"id-{name}", "policy_name": name, "verdict": verdict, "conditions": conds, "benefit": b,
            "apply": {"apply_end_date": None, "recruit_status": "always", "url": "https://example.test"}}


def policy_result(findings=(), other=(), situational=()):
    return {"case_id": "t", "profile_version": "v1", "generated_at": "x", "search_scope": {"policy_db_as_of": "x"},
            "findings": list(findings), "other_findings": list(other), "situational_findings": list(situational),
            "questions": []}


def row(section, item, won, detail=None):
    return {"section": section, "item": item, "monthly_won": won, "purpose": "생활", "necessity": None,
            "detail": detail}


CASHFLOW = [
    row("변동지출", "생활비", 700_000, "식재료 28 + 직장 식사 3 + 외식 20(만원)"),
    row("변동지출", "문화생활비", 200_000),
    row("고정지출", "통신비", 50_000),
    row("고정지출", "교통비", 100_000, "대중교통 8 + 택시 2(만원)"),
    row("보험", "보장성보험", 100_000),
    row("저축", "적금", 300_000),
]


def profile(cashflow=CASHFLOW):
    facts = {"birth_date": "1992-04-18", "sex": "남", "region_sido": "경기", "marital_status": "미혼",
             "home_owner": False}
    return {"case_id": "t", "profile_version": "v1", "as_of_date": "2026-11-12",
            "facts": {k: {"value": v} for k, v in facts.items()}, "goals": [],
            "cashflow_items": copy.deepcopy(list(cashflow)), "liabilities": []}


# 프로필 변동 90만·고정+보험 25만과 맞춘 step2(만원)
STEP2 = {"p": 0.0, "q": 0.0, "r": 0.0, "s": 0.0, "M": 3000.0, "deltaM": 1200.0,
         "_meta": {"deltaM_detail": {"income_monthly": 400.0, "fixed_after_monthly": 25.0,
                                     "variable_after_monthly": 80.0, "cut_monthly": 10.0},
                   "asset_detail": {"M_items": ["현금 3000"]}}}

MEAL = finding("직장인 든든한 한끼", "확인 필요", "기타", "[기타] 월 4만원", open_cond=True, monthly_won=40_000)


def line(res, item):
    return next(l for l in res["lines"] if l["item"] == item)


# ① 항목 상한: 적용 부분(직장 식사 3만원)을 넘지 않음
def test_saving_capped_by_applicable_part():
    l = line(spending_cut(profile(), policy_result(other=[MEAL])), "생활비")
    assert l["category"] == "식비" and l["applicable_part_won"] == 30_000
    assert l["potential_saving_won"] == 30_000 and l["policies"][0]["monthly_saving_won"] == 40_000
    assert l["after_potential_won"] == 670_000


def test_saving_capped_by_expense():
    big = finding("통신 요금 할인", monthly_won=200_000)
    assert line(spending_cut(profile(), policy_result([big])), "통신비")["certain_saving_won"] == 50_000


# ② 같은 항목은 가장 큰 정책 하나만
def test_only_largest_policy_counts():
    res = spending_cut(profile(), policy_result([finding("통신 요금 할인 A", monthly_won=10_000),
                                                 finding("통신 요금 할인 B", monthly_won=20_000)]))
    l = line(res, "통신비")
    assert len(l["policies"]) == 2 and l["certain_saving_won"] == 20_000
    assert res["totals"]["fixed_certain"]["monthly_won"] == 20_000


# ③ 금액 모름 → null, 합계 제외
def test_unknown_amount_is_null_and_excluded():
    res = spending_cut(profile(), policy_result([finding("문화 관람 할인")]))
    l = line(res, "문화생활비")
    assert l["policies"][0]["monthly_saving_won"] is None and l["policies"][0]["note"] == "공고 확인"
    assert l["potential_saving_won"] == 0 and res["totals"]["variable_potential"]["monthly_won"] == 0


# ④ certain과 potential 구분
def test_certain_vs_potential():
    res = spending_cut(profile(), policy_result([finding("통신 요금 할인 A", monthly_won=10_000),
                                                 finding("통신 요금 할인 B", "확인 필요", monthly_won=30_000)]))
    l = line(res, "통신비")
    assert l["certain_saving_won"] == 10_000 and l["potential_saving_won"] == 30_000
    assert l["certain_policy"] == "통신 요금 할인 A" and l["potential_policy"] == "통신 요금 할인 B"


# ⑤ 기간이 있으면 연간 효과 = 월액 × min(기간, 12)
@pytest.mark.parametrize("months,annual", [(6, 60_000), (24, 120_000), (None, 120_000)])
def test_annual_effect_uses_duration(months, annual):
    res = spending_cut(profile(), policy_result([finding("통신 요금 할인", monthly_won=10_000,
                                                         duration_months=months)]))
    l = line(res, "통신비")
    assert l["policies"][0]["duration_months"] == months and l["annual_certain_won"] == annual


# ⑥ step2 불일치 경고(기준은 step2)
def test_step2_mismatch_warns():
    s = copy.deepcopy(STEP2)
    s["_meta"]["deltaM_detail"]["variable_after_monthly"] = 150.0
    res = spending_cut(profile(), policy_result(), s)
    assert any("변동지출" in w and "step2 값을 기준" in w for w in res["warnings"])
    assert res["step2_link"]["compare_variable"]["step2"] == 160.0
    assert not any("고정지출+보험" in w for w in res["warnings"])


def test_step2_consistent_no_warning():
    res = spending_cut(profile(), policy_result(), copy.deepcopy(STEP2))
    assert res["warnings"] == []


# ⑦ 변동지출 정책 절감 상한 = variable_after_monthly
def test_variable_saving_capped_by_step2():
    s = copy.deepcopy(STEP2)
    s["_meta"]["deltaM_detail"].update(variable_after_monthly=1.0, cut_monthly=89.0)
    res = spending_cut(profile(), policy_result(other=[MEAL]), s)
    link = res["step2_link"]
    assert link["policy_saving_monthly"]["potential"]["variable"] == 1.0
    assert link["variable_after_policy"]["potential"] == 0.0
    assert link["behavior_cut_monthly"] == 89.0
    assert any("상한 적용" in w for w in res["warnings"])


# ⑧ deltaM_after와 allocation_effect 전후
def test_deltaM_after_and_allocation_effect():
    pr = policy_result([finding("K-패스", discount_pct=30.0)], other=[MEAL])
    res = spending_cut(profile(), pr, copy.deepcopy(STEP2))
    link, eff = res["step2_link"], res["allocation_effect"]
    # 교통: 대중교통 8만 × 30% = 2.4만(certain), 식비 3만(potential만)
    assert link["policy_saving_monthly"]["certain"] == {"variable": 0.0, "fixed": 2.4}
    assert link["deltaM_after"]["certain"] == {"value": 1200 + 28.8, "from_variable": 0.0, "from_fixed": 28.8}
    assert link["deltaM_after"]["potential"]["value"] == pytest.approx(1200 + 28.8 + 36.0)
    assert eff["before"]["monthly_surplus"] == 100.0
    assert eff["after_certain"]["monthly_surplus"] == pytest.approx(1228.8 / 12, abs=0.01)
    assert eff["after_potential"]["monthly_needed_total"] == eff["before"]["monthly_needed_total"]
    assert eff["after_potential"]["shortfall_change"] <= eff["after_certain"]["shortfall_change"] <= 0


# ⑨ step2 없이 실행
def test_runs_without_step2():
    res = spending_cut(profile(), policy_result(other=[MEAL]))
    assert res["step2_link"] is None and res["allocation_effect"] is None
    assert [l["section"] for l in res["lines"]][:2] == ["변동지출", "변동지출"]
    assert "저축" not in {l["section"] for l in res["lines"]}
    json.dumps(res, ensure_ascii=False)


# ⑩ 상황형은 절감에 안 들어가고 to_check에만
def test_situational_not_counted():
    lunch = finding("직장인 든든한 점심밥", "입력상 충족", "할인·환급", "[할인·환급] 최대 4만원, 지출의 20%",
                    discount_pct=20.0, total_won=40_000)
    res = spending_cut(profile(), policy_result(situational=[lunch]))
    assert res["totals"]["variable_potential"]["monthly_won"] == 0
    assert line(res, "생활비")["policies"] == []
    check = next(c for c in res["to_check"] if c["item"] == "생활비")
    assert check["policies"][0]["policy_name"] == "직장인 든든한 점심밥" and check["policies"][0]["kind"].startswith("상황형")


def test_to_check_max_five():
    many = [finding(f"식비 지원 {i}", "확인 필요", "바우처·현물") for i in range(7)]
    check = next(c for c in spending_cut(profile(), policy_result(many))["to_check"] if c["item"] == "생활비")
    assert len(check["policies"]) == 5 and check["more"] == 2


def test_one_off_and_savings_kept_separate():
    pr = policy_result([finding("문화 패스", btype="바우처·현물", total_won=150_000),
                        finding("청년 매칭 통장", btype="매칭저축", own_saving_monthly_won=100_000, match_ratio=1.0),
                        finding("청년 전세대출", btype="대출")])
    res = spending_cut(profile(), pr)
    assert res["separate"]["counts"] == {"one_off": 1, "savings": 1, "debt": 1}
    assert line(res, "문화생활비")["potential_saving_won"] == 0


# ⑪ handoff 기본 호출은 그대로, 확장 인자를 줄 때만 달라짐
def test_handoff_default_unchanged():
    pr = policy_result([finding("K-패스", discount_pct=30.0)], other=[MEAL])
    cf = profile()["cashflow_items"]
    default = build(copy.deepcopy(pr), copy.deepcopy(cf), [])
    explicit = build(copy.deepcopy(pr), copy.deepcopy(cf), [], categories=CATEGORIES, other_filter=None)
    extended = build(copy.deepcopy(pr), copy.deepcopy(cf), [], categories=EXT_CATEGORIES, other_filter=ext_other_filter)
    for d in (default, explicit, extended):
        d.pop("generated_at")
    assert default == explicit
    food = lambda d: next(l for l in d["expense_lines"] if l["item"] == "생활비")
    assert food(default)["category"] is None and food(default)["policy_offsets"] == []
    assert food(extended)["category"] == "식비" and food(extended)["policy_offsets"][0]["policy_name"] == MEAL["policy_name"]
    assert default["totals"]["potential_monthly_saving_won"] == 24_000
    assert extended["totals"]["potential_monthly_saving_won"] == 24_000 + 40_000


def test_wedding_expense_policy_not_food():
    assert not ext_other_filter(finding("공공예식장 예식비 지원", btype="기타"))
