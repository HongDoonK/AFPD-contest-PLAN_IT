"""남는 잉여의 주거 전환·추가 노후 재원, 노후 재원 계좌 배분과 수령 세금 구간(retire_tax) 시험."""
import copy

import pytest

from customer.profile import load_case
from planning.retire_tax import retire_tax_plan
from planning.strategy import assumptions_of, rebalance_phases, stage_of, _monthly_real

from test_transition import KIM_POLICY, ROOT, ZERO, g, strategy_for


@pytest.fixture(scope="module")
def kim():
    if not KIM_POLICY.exists():
        pytest.skip("정책 결과 없음")
    return strategy_for(load_case(ROOT / "customer" / "cases" / "kim_doyun.json"))


# ── A. 남는 잉여 처리(합성 사례, 수익률 0) ──────────────────────────
def test_overflow_to_housing_then_late_funded_then_extra():
    goals = [g("housing", 3000, 12), g("wedding", 5000, 12), g("retire", 600, 60)]
    out = rebalance_phases(goals, 100, ZERO, initial_ratios={"housing": 50.0, "wedding": 30.0, "retire": 20.0},
                           overflow_key="housing", extra_horizon=60)
    ph = out["phases"]
    # 12달: 주거 600, 결혼 360, 노후 240 → 주거·결혼 시점 도래, 노후 100% → 16달에 노후 640(≥ 600) 적립 완료
    assert ph[1]["events"] == ["housing 목표 시점", "wedding 목표 시점"] and ph[1]["ratios"] == {"retire": 100.0}
    over = ph[2]
    assert over["events"] == ["retire 적립 완료", "주거 추가 적립(목표 시점 이후)"]
    assert over["start_month"] == 16 and over["mode"] == "housing" and over["ratios"] == {"housing": 100.0}  # 결혼으로는 돌리지 않음
    h = out["goals"]["housing"]
    assert h["shortfall_at_due"]["base"] == pytest.approx(2400)
    # 주거 600 + 100 × 24달 = 3000 → 지연 달성
    assert h["late_funded_month"] == 40
    late = ph[3]
    assert late["events"] == ["housing 지연 달성", "추가 노후 재원 적립"] and late["ratios"] == {"retire_extra": 100.0}
    assert out["extra"]["start_month"] == late["start_month"]
    assert out["extra"]["balance"]["base"] == pytest.approx(100 * (60 - late["start_month"]))
    assert all(p["unassigned"] == 0 for p in ph)
    assert out["goals"]["wedding"]["late_funded_month"] is None and sum(out["goals"]["wedding"]["contrib"][12:]) == 0


def test_no_housing_goes_to_extra_retire():
    out = rebalance_phases([g("wedding", 5000, 12), g("retire", 300, 36)], 100, ZERO,
                           initial_ratios={"wedding": 50.0, "retire": 50.0}, overflow_key=None, extra_horizon=36)
    last = out["phases"][-1]
    # 6달에 노후 적립 완료 → 결혼 100% → 12달 결혼 시점 도래 뒤 주거가 없으니 추가 노후 재원
    assert last["events"] == ["wedding 목표 시점", "추가 노후 재원 적립"] and last["start_month"] == 12 and last["ratios"] == {"retire_extra": 100.0}
    assert last["unassigned"] == 0 and out["extra"]["balance"]["base"] == pytest.approx(100 * (36 - last["start_month"]))


def test_housing_already_funded_at_due_goes_to_extra():
    out = rebalance_phases([g("housing", 500, 12), g("retire", 2000, 24)], 100, ZERO,
                           initial_ratios={"housing": 50.0, "retire": 50.0}, overflow_key="housing", extra_horizon=48)
    assert not any(p["mode"] == "housing" for p in out["phases"])
    assert out["goals"]["housing"]["late_funded_month"] is None


# ── B. retire_tax(합성) ────────────────────────────────────────────
def _goal(need, living, pension, period, horizon=120, allocated=0.0):
    return {"need": need, "horizon": horizon, "allocated": allocated,
            "inputs": {"living_min": living, "pension": pension, "years_before_pension": 0, "years_with_pension": period}}


def _phases(*starts):
    return [{"phase": i + 1, "start_month": m, "start": f"m{m}"} for i, m in enumerate(starts)]


def test_qualified_cap_and_stop():
    a = ZERO
    goal = _goal(60000, 300, 100, 20)  # 연 필요 인출 2,400, 적격 상한 1,500 × 20 = 30,000
    contrib = [500.0] * 120
    rt = retire_tax_plan(goal, {"q": 0, "r": 0, "s": 0}, contrib, [], _phases(0, 60), a, stage_of, _monthly_real)
    assert rt["annual_need"] == 2400 and rt["qualified_cap"] == 30000 and rt["target_qualified"] == 30000
    assert rt["target_nonqualified"] == 30000 and rt["nonqualified_annual"] == 900
    q = next(x for x in rt["accounts"] if x["key"] == "qualified")
    assert q["balance_at_retire"] == pytest.approx(9000)  # 월 75 × 120 (연 900 공제 한도), 상한 미도달
    assert q["verdict"] == "한도 이내"
    # 계좌 배분 합 = 노후 월 적립
    for x in rt["by_phase"]:
        assert x["qualified"] + x["insurance"] + x["isa"] + x["general"] == pytest.approx(x["total"], abs=0.02)
        assert x["total"] == pytest.approx(500)
    assert rt["by_phase"][0]["qualified"] == 75 and rt["by_phase"][0]["insurance"] == 150
    assert rt["by_phase"][0]["isa"] == pytest.approx(166.67, abs=0.01)


def test_qualified_stops_at_cap():
    goal = _goal(3000, 200, 100, 2)  # 상한 1,500 × 2 = 3,000
    rt = retire_tax_plan(goal, {"q": 2000, "r": 0, "s": 0}, [100.0] * 120, [], _phases(0), ZERO, stage_of,
                         _monthly_real)
    q = next(x for x in rt["accounts"] if x["key"] == "qualified")
    assert q["balance_at_retire"] == pytest.approx(3000) and q["stopped_month"] is not None
    assert q["annual_withdrawal"] == pytest.approx(1500) and q["verdict"] == "한도 이내"


def test_general_account_balance_cap_and_over_verdict():
    goal = _goal(200000, 500, 0, 20)
    rt = retire_tax_plan(goal, {}, [1000.0] * 120, [], _phases(0), ZERO, stage_of, _monthly_real)
    assert rt["general_balance_cap"] == 40000  # 2,000 ÷ 5%
    gen = next(x for x in rt["accounts"] if x["key"] == "general")
    # 월 1,000 − 75 − 150 − 166.67 = 608.33 × 120
    assert gen["balance_at_retire"] == pytest.approx(608.33 * 120, abs=1)
    assert gen["interest"] == pytest.approx(gen["balance_at_retire"] * 0.05, abs=0.01)
    assert gen["verdict"].startswith("초과 예상") and any("2,000" in w for w in rt["warnings"])


# ── 김도윤 검산 ────────────────────────────────────────────────────
def test_kim_retire_tax_check(kim):
    rt = kim["retire_tax"]
    assert rt["annual_need"] == pytest.approx(1559.49, abs=0.01) and rt["period_years"] == 18.7
    assert rt["qualified_cap"] == 28050 and rt["target_qualified"] == 28050
    assert rt["target_nonqualified"] == pytest.approx(1112.42, abs=0.01)
    assert rt["nonqualified_annual"] == pytest.approx(59.49, abs=0.01)
    acc = {x["key"]: x for x in rt["accounts"]}
    assert acc["qualified"]["balance_at_retire"] <= 28050 + 0.01 and acc["qualified"]["verdict"] == "한도 이내"
    assert acc["general"]["verdict"] == "한도 이내"
    # 단계별 계좌 배분 합 = 노후 월 적립(추가 노후 재원 포함)
    for x, ph in zip(rt["by_phase"], kim["phases"]):
        want = ph["monthly"].get("retire", 0) + ph["monthly"].get("retire_extra", 0)
        assert x["total"] == pytest.approx(want, abs=0.02)
        assert x["qualified"] + x["insurance"] + x["isa"] + x["general"] == pytest.approx(x["total"], abs=0.03)
    card = next(c for c in kim["cards"] if c["id"] == "L5")
    assert card["title"] == "노후 수령 세금 구간 관리" and card["term"] == "장기"
    assert any("세무 자문이 아님" in x for x in card["confirm"])


def test_kim_housing_late_funded(kim):
    h = next(x for x in kim["goals"] if x["goal"] == "housing")
    assert h["late_funded_month"] == "2049-03"
    m1 = next(c for c in kim["cards"] if c["id"] == "M1")
    assert "노후 적립 완료 뒤 2038-08부터 월 87.1만원이 주거로 넘어와 2049-03에 필요자금에 닿습니다" in m1["action"]
    assert m1["numbers"]["지연 달성 예상 시점"] == "2049-03"
    dec = next(d for d in kim["decisions"] if d["topic"].startswith("주거 목표 시점 부족액"))
    assert "지연 달성 예상 2049-03" in dec["detail"]
    assert kim["extra_retire"]["start"] == "2049-03" and kim["extra_retire"]["balance"]["base"] > 0


def test_retire_tax_reference_values():
    a = assumptions_of()
    from planning.reference import STRATEGY_REFERENCE
    for k in ("private_pension_separate_limit", "financial_income_limit", "pension_contribution_limit",
              "savings_insurance_monthly_limit"):
        assert STRATEGY_REFERENCE[k]["kind"] == "제도" and STRATEGY_REFERENCE[k]["check"] == "원문 확인 필요"
    assert STRATEGY_REFERENCE["general_account_yield"]["kind"] == "가정"
    assert a["private_pension_separate_limit"] == 1500 and a["financial_income_limit"] == 2000


def test_kim_l1_l2_linked_and_advisor_facts(kim):
    from planning.advisor import _facts
    cards = {c["id"]: c for c in kim["cards"] if c}
    assert any("L5" in x for x in cards["L1"]["confirm"]) and any("L5" in x for x in cards["L2"]["confirm"])
    facts = _facts([cards["L5"]])
    assert "노후 수령 세금 구간 관리 연 필요 인출액: 1,559.49만원" in facts
    assert any(f.startswith("노후 수령 세금 구간 관리 적격 목표 잔액 상한") for f in facts)
