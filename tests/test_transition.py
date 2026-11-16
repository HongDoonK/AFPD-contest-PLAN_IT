"""양육 전환(T_c부터 양육비를 변동지출로, 남은 목표 비율 재조정) 시험."""
import copy
import json
from pathlib import Path

import pytest

from customer.profile import load_case
from planning.reflect import build
from planning.spending_cut import spending_cut
from planning.step2_build import build_step2
from planning.strategy import build_strategy

ROOT = Path(__file__).resolve().parents[1]
KIM_STEP2 = json.loads((ROOT / "planning" / "cases" / "kim_doyun_step2.json").read_text(encoding="utf-8"))
KIM_POLICY = ROOT / "output" / "policy_kim_doyun_v2.json"
needs = pytest.mark.skipif(not KIM_POLICY.exists(), reason="정책 결과 없음")


def strategy_for(profile, step2=None, policy_path=KIM_POLICY, assumptions=None):
    step2 = copy.deepcopy(step2 or KIM_STEP2)
    pol = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    kw = {"goal_basis": "customer"}
    ref = build(copy.deepcopy(step2), profile, pol, "certain", **kw)
    cut = spending_cut(profile, pol, copy.deepcopy(step2), allocate_kwargs=kw)
    return build_strategy(ref, profile, pol, cut, assumptions=assumptions, **kw)


@pytest.fixture(scope="module")
def kim():
    if not KIM_POLICY.exists():
        pytest.skip("정책 결과 없음")
    return strategy_for(load_case(ROOT / "customer" / "cases" / "kim_doyun.json"))


@needs
def test_child_support_kim(kim):
    cost = kim["child_transition"]["child_cost"]
    assert cost["income_monthly"] == 480 and cost["bracket"] == "400~499만원"
    assert cost["by_age_thousand_won"] == {"0~2세": 1098, "3~5세": 1113, "6~8세": 1140, "9~11세": 1163,
                                           "12~14세": 1280, "15~18세": 1402}
    assert cost["average"] == pytest.approx(119.93, abs=0.01) and cost["children_factor"] == 1.065
    assert cost["monthly"] == pytest.approx(63.86, abs=0.01)


@needs
def test_transition_point_statistical(kim):
    tr = kim["child_transition"]
    assert tr["date"] == "2028-04" and tr["source"] == "통계 기본값(확인 필요)"
    assert any(d["topic"] == "양육 전환 시점" for d in kim["decisions"])
    assert any(d["topic"] == "양육 준비금 처리" for d in kim["decisions"])
    assert any(r["item"] == "출산 계획 시점" for r in kim["data_requests"])


@needs
def test_transition_point_from_customer_goal():
    p = copy.deepcopy(load_case(ROOT / "customer" / "cases" / "kim_doyun.json"))
    p["goals"].append({"goal_id": "g-baby", "term": "중기", "name": "출산·양육 준비", "due": "2029-06",
                       "amount_won": 30_000_000, "amount_basis": "일시금", "status": "기재", "raw": None})
    tr = strategy_for(p)["child_transition"]
    assert tr["date"] == "2029-06" and tr["source"] == "고객 목표"


@needs
def test_phases_ratios_and_monthly(kim):
    """김도윤: 시작 → 양육 전환 → 결혼 목표 시점 → 주거 목표 시점 → 노후 달성 → 자녀 19세."""
    ph = kim["phases"]
    assert [(p["start"], p["events"]) for p in ph] == [
        ("2026-11", ["시작"]), ("2028-04", ["양육 전환"]), ("2029-11", ["결혼 목표 시점"]),
        ("2031-11", ["주거 목표 시점"]), ("2038-08", ["노후 적립 완료", "주거 추가 적립(목표 시점 이후)"]),
        ("2047-04", ["자녀 19세(양육비 종료)"]), ("2049-03", ["주거 지연 달성", "추가 노후 재원 적립"])]
    assert ph[0]["ratios"] == {"housing": 52.9, "wedding": 13.4, "child": 21.3, "retire": 12.4}
    assert ph[1]["ratios"] == {"housing": 66.3, "wedding": 20.0, "retire": 13.7}
    assert ph[2]["ratios"] == {"housing": 88.5, "retire": 11.5} and ph[3]["ratios"] == {"retire": 100.0}
    # 노후 적립 완료 뒤 잉여는 미배정 대신 주거(목표 시점 이후)로, 지연 달성 뒤 추가 노후 재원으로
    assert ph[4]["ratios"] == {"housing": 100.0} and ph[4]["monthly"]["housing"] == pytest.approx(87.14, abs=0.01)
    assert ph[5]["surplus"] == 151.0 and ph[5]["ratios"] == {"housing": 100.0} and ph[5]["unassigned"] == 0
    assert ph[6]["ratios"] == {"retire_extra": 100.0} and all(p["unassigned"] == 0 for p in ph)
    assert not any(k in p["ratios"] for p in ph[4:] for k in ("wedding", "child"))
    for p in ph:
        if p["ratios"]:
            assert sum(p["ratios"].values()) == pytest.approx(100.0)
            assert sum(p["monthly"].values()) == pytest.approx(p["surplus"], abs=0.03)
    goals = {g["goal"]: g for g in kim["goals"]}
    assert goals["wedding"]["fv_base"] == pytest.approx(1158, abs=2) and goals["wedding"]["shortfall_at_due"]["base"] == pytest.approx(1842, abs=2)
    assert goals["housing"]["fv_base"] == pytest.approx(6811, abs=2) and goals["housing"]["shortfall_at_due"]["base"] == pytest.approx(13189, abs=2)
    assert goals["retire"]["achieved_month"] == "2038-08" and goals["retire"]["shortfall_at_due"]["base"] == 0
    assert 1.0 <= goals["retire"]["coverage"] < 1.01  # 적립 완료 뒤 굴린 잔액이 필요자금을 조금 넘는 수준
    assert goals["retire"]["funded_note"] == "이 시점 이후 적립 없이도 목표 시점에 필요자금 도달"
    child = goals["child"]
    assert child["ends_at_transition"] and child["required_monthly"] is None
    assert child["transition"]["reserve"]["base"] == child["fv_base"]
    assert child["transition"]["reserve_months"] == pytest.approx(child["fv_base"] / 63.86, abs=0.1)
    assert kim["summary"]["phase2"]["surplus"] == ph[1]["surplus"]
    assert any(d["topic"].startswith("주거 목표 시점 부족액") for d in kim["decisions"])
    card = next(c for c in kim["cards"] if c["goal"] == "child")
    assert card["numbers"]["전환 시점"] == "2028-04" and "63.9만원" in card["action"]
    l2 = next(c for c in kim["cards"] if c["id"] == "L2")
    assert "월 적립이 18.7만원에서 87.1만원으로 바뀝니다" in l2["action"] and "2038-08부터는 적립 없이도" in l2["action"]


@needs
def test_negative_phase2_surplus_is_zero():
    st = strategy_for(load_case(ROOT / "customer" / "cases" / "kim_doyun.json"),
                      assumptions={"child_support_self_share": 5.0})
    ph = next(p for p in st["phases"] if "양육 전환" in p["events"])
    assert ph["surplus"] == 0 and any("음수" in w for w in st["warnings"])


def test_children_present_no_transition():
    case = ROOT / "customer" / "cases" / "auto" / "blank_04.json"
    pol = ROOT / "output" / "policy_blank_04_v1.json"
    if not case.exists() or not pol.exists():
        pytest.skip("blank_04 파일 없음")
    p = load_case(case)
    st = strategy_for(p, build_step2(p), pol)
    assert st["child_transition"] is None
    assert not any("양육 전환" in e for p in st["phases"] for e in p["events"])
    assert "현재 지출" in st["child_note"]
    assert any(g["name"] == "교육자금" and g["kind"] == "extra" for g in st["goals"])


# ── 일반화한 단계별 재계산(합성 사례, 손 검산) ─────────────────────
from planning.strategy import assumptions_of, rebalance_phases  # noqa: E402

FLAT = {"low": 0.0, "base": 0.0, "high": 0.0}
ZERO = assumptions_of({"short_return": FLAT, "growth_return": FLAT, "inflation": 0.0,
                       "retire_stages": [{**s, "rate": FLAT} for s in assumptions_of()["retire_stages"]]})


def g(key, need, months, start=0.0):
    return {"key": key, "name": key, "start": start, "need": need, "months": months, "horizon": months,
            "kind": "growth", "child": False}


def test_two_goals_first_due_then_other_gets_100():
    out = rebalance_phases([g("A", 1000, 12), g("B", 10000, 48)], 100, ZERO, initial_ratios={"A": 50.0, "B": 50.0})
    ph = out["phases"]
    assert [p["events"] for p in ph] == [["시작"], ["A 목표 시점"], ["B 목표 시점"]]
    assert ph[0]["ratios"] == {"A": 50.0, "B": 50.0} and ph[1]["ratios"] == {"B": 100.0}
    a, b = out["goals"]["A"], out["goals"]["B"]
    assert a["due_balance"]["base"] == pytest.approx(600) and a["shortfall_at_due"]["base"] == pytest.approx(400)
    assert b["due_balance"]["base"] == pytest.approx(600 + 100 * 36)  # 12달 × 50 + 36달 × 100
    assert sum(a["contrib"][12:]) == 0 if len(a["contrib"]) > 12 else True


def test_achieved_goal_stops_and_rest_reweighted():
    out = rebalance_phases([g("A", 300, 24), g("B", 10000, 48)], 100, ZERO, initial_ratios={"A": 50.0, "B": 50.0})
    ph = out["phases"]
    assert ph[1]["events"] == ["A 적립 완료"] and ph[1]["start_month"] == 6 and ph[1]["ratios"] == {"B": 100.0}
    a = out["goals"]["A"]
    assert a["achieved_month"] == 6 and sum(a["contrib"][6:]) == 0
    assert a["due_balance"]["base"] == pytest.approx(300) and a["shortfall_at_due"]["base"] == 0


def test_same_month_events_grouped_and_unassigned_when_none_left():
    out = rebalance_phases([g("A", 1000, 12), g("B", 1000, 12)], 100, ZERO, initial_ratios={"A": 50.0, "B": 50.0})
    ph = out["phases"]
    assert ph[1]["events"] == ["A 목표 시점", "B 목표 시점"] and len(ph) == 2
    assert ph[1]["ratios"] == {} and ph[1]["unassigned"] == 100


def test_formula_at_start_when_goal_already_met():
    out = rebalance_phases([g("A", 100, 12, start=200), g("B", 1200, 24)], 100, ZERO,
                           initial_ratios={"A": 30.0, "B": 70.0})
    ph = out["phases"]
    assert ph[0]["events"] == ["A 적립 완료"] and ph[0]["ratios"] == {"B": 100.0}  # 시작 달 사건이면 공식으로 다시


def test_child_transition_and_cost_in_simulation():
    child = {**g("C", 99999, 6), "child": True, "horizon": 120}
    out = rebalance_phases([child, g("B", 5000, 60)], 100, ZERO, child_cost=40, tc=6, t3=30,
                           initial_ratios={"C": 50.0, "B": 50.0})
    ph = out["phases"]
    assert [p["events"] for p in ph][:3] == [["시작"], ["양육 전환"], ["자녀 19세(양육비 종료)"]]
    assert ph[1]["surplus"] == 60 and ph[1]["ratios"] == {"B": 100.0} and ph[2]["surplus"] == 100
    assert out["goals"]["C"]["due_balance"]["base"] == pytest.approx(300)  # 6달 × 50


def test_funded_rule_with_returns():
    """적립 완료 = 그 달 잔액을 적립 없이 목표 시점까지 굴려 need 이상이 되는 첫 달(한 달 전에는 미달)."""
    from planning.strategy import stage_of, _monthly_real
    a = assumptions_of({"inflation": 0.0})
    goal = {"key": "A", "name": "A", "start": 0.0, "need": 2000.0, "months": 120, "horizon": 120, "kind": "growth",
            "child": False}
    out = rebalance_phases([goal, g("B", 1e6, 120)], 100, a, initial_ratios={"A": 50.0, "B": 50.0})
    m = out["goals"]["A"]["achieved_month"]
    path = out["goals"]["A"]["path"]["base"]

    def grow(k):
        f = 1.0
        for j in range(k, 120):
            f *= 1 + _monthly_real(stage_of("growth", (120 - j) / 12, a)["rate"]["base"], a)
        return f
    assert path[m] * grow(m) >= 2000 - 1e-6 > path[m - 1] * grow(m - 1)
    assert sum(out["goals"]["A"]["contrib"][m:]) == 0
    assert out["goals"]["A"]["due_balance"]["base"] == pytest.approx(path[m] * grow(m))


def test_plan_state():
    from planning.goals import plan_state
    assert plan_state("없음") == "none" and plan_state("계획 없음") == "none" and plan_state("비혼") == "none"
    assert plan_state("딩크") == "none" and plan_state("자녀 계획 없음") == "none"
    assert plan_state("당장 계획 없음") == "hedged" and plan_state("아직 미정") == "hedged"
    assert plan_state("생각 중") == "hedged" and plan_state(None) is None and plan_state("") is None


def test_hedged_marriage_plan_keeps_base_goal():
    from planning.allocation import allocate
    p = copy.deepcopy(load_case(ROOT / "customer" / "cases" / "kim_doyun.json"))
    p["goals"] = [x for x in p["goals"] if "결혼" not in x["name"]]
    p["facts"]["marriage_plan"] = {"value": "당장 계획 없음", "status": "기재"}
    a = allocate(copy.deepcopy(KIM_STEP2), p, goal_basis="customer")
    assert any(x["goal"] == "wedding" for x in a["goals"])
    assert any("유보 표현" in r["item"] for r in a["data_requests"])
    p["facts"]["marriage_plan"] = {"value": "비혼", "status": "기재"}
    a = allocate(copy.deepcopy(KIM_STEP2), p, goal_basis="customer")
    assert not any(x["goal"] == "wedding" for x in a["goals"])
    assert any(i.get("plan_use") == "고객이 계획 없음으로 기재해 제외" for i in a["goals_input"])


def test_irregular_income_emergency_excludes_income_tax():
    from planning.allocation import _monthly_spending, allocate
    case = ROOT / "customer" / "cases" / "lee_seojun.json"
    if not case.exists():
        pytest.skip("이서준 없음")
    p = load_case(case)
    spend, used, dropped = _monthly_spending(p)
    assert dropped == ["종합·지방세 월 적립 505.2"] and "국민연금" in used and "건강·장기요양" in used
    assert spend == pytest.approx(594.13, abs=0.01)
    a = allocate(build_step2(p), p, goal_basis="customer")
    assert a["m_avail"]["earmarked"] == 1623 and a["m_avail"]["value"] == pytest.approx(19100, abs=0.01)
    assert a["emergency"]["amount"] == pytest.approx(594.13 * 12, abs=0.1)
    assert a["emergency"]["source"].startswith("소득 불규칙")


def test_child_weight_uses_original_horizon_before_transition():
    """전환 전 재계산에서도 양육 years = 원래 양육 목표 시점까지(전환 시점까지가 아님)."""
    child = {**g("C", 10000, 36), "child": True, "horizon": 150}
    out = rebalance_phases([child, g("A", 150, 6), g("B", 3000, 60)], 100, ZERO, tc=36,
                           initial_ratios={"C": 40.0, "A": 30.0, "B": 30.0})
    ph = out["phases"][1]
    assert ph["events"] == ["A 적립 완료"] and ph["start_month"] == 5
    bal_c, bal_b = ph["start_balances"]["C"]["base"], ph["start_balances"]["B"]["base"]
    wc, wb = (10000 - bal_c) / ((150 - 5) / 12), (3000 - bal_b) / ((60 - 5) / 12)
    assert ph["ratios"]["C"] == pytest.approx(wc / (wc + wb) * 100, abs=0.1)
    assert ph["ratios"]["C"] < 60  # 전환 시점(31달 뒤, 하한 1년 적용 전 2.6년)으로 나눴다면 약 79%


@pytest.mark.parametrize("case,version", [("lee_seojun", "v2")])
def test_no_child_ratio_jump_real(case, version):
    path = ROOT / "customer" / "cases" / f"{case}.json"
    pol = ROOT / "output" / f"policy_{case}_{version}.json"
    if not path.exists() or not pol.exists():
        pytest.skip("파일 없음")
    p = load_case(path)
    st = strategy_for(p, build_step2(p), pol)
    before = [ph for ph in st["phases"] if "child" in ph["ratios"]]
    assert len(before) >= 2
    assert before[1]["ratios"]["child"] < before[0]["ratios"]["child"] * 2
