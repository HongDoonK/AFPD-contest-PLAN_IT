"""planning.strategy 시험. 손으로 검산할 수 있는 합성 사례와 김도윤·빈칸형 실제 파일(있으면)."""
import copy
import json
import math
from pathlib import Path

import pytest

from customer.profile import load_case
from planning.reflect import build as reflect_build
from planning.spending_cut import spending_cut
from planning.step2_build import build_step2
from planning.strategy import (annuity_payment, asset_mapping, assumptions_of, build_strategy, by_ratio, pension_credit,
                               project, rate_kind, rate_plan, real_rate, required_monthly, sequential, stage_of, term_of)

ROOT = Path(__file__).resolve().parents[1]
FLAT = {"low": 0.0, "base": 0.0, "high": 0.0}
ZERO = assumptions_of({"short_return": FLAT, "growth_return": FLAT, "inflation": 0.0,
                       "retire_stages": [{**s, "rate": FLAT} for s in assumptions_of()["retire_stages"]]})


# ── 수식 ──────────────────────────────────────────────────────
def test_real_rate():
    assert real_rate(0.025, 0.02) == pytest.approx(1.025 / 1.02 - 1)
    assert real_rate(0.02, 0.02) == pytest.approx(0.0)


@pytest.mark.parametrize("years,expect", [(None, None), (1, "단기"), (3, "단기"), (3.01, "중기"), (10, "중기"), (10.5, "장기")])
def test_term_of(years, expect):
    assert term_of(years) == expect


@pytest.mark.parametrize("cat,expect", [("retire", "retire"), ("debt", "short"), ("emergency", "short"),
                                        ("housing", "growth"), ("child", "growth"), ("wedding", "growth"),
                                        ("other", "growth")])
def test_rate_kind(cat, expect):
    assert rate_kind(cat) == expect


@pytest.mark.parametrize("kind,rem,name,base,risk", [
    ("growth", 3.01, "성장 운용", 0.065, None), ("growth", 3.0, "단기 운용", 0.025, None),
    ("short", 30, "단기 운용", 0.025, None),
    ("retire", 25, "1단계", 0.10, 0.7), ("retire", 20.01, "1단계", 0.10, 0.7), ("retire", 20, "2단계", 0.08, 0.5),
    ("retire", 10.01, "2단계", 0.08, 0.5), ("retire", 10, "3단계", 0.05, 0.3), ("retire", 0, "3단계", 0.05, 0.3)])
def test_stage_boundaries(kind, rem, name, base, risk):
    st = stage_of(kind, rem, assumptions_of())
    assert st["name"] == name and st["rate"]["base"] == pytest.approx(base) and st["risk"] == risk


def test_user_rate_ranges():
    a = assumptions_of()
    assert a["short_return"] == {"low": 0.02, "base": 0.025, "high": 0.03}
    assert a["growth_return"] == {"low": 0.05, "base": 0.065, "high": 0.08}
    assert [s["rate"]["base"] for s in a["retire_stages"]] == [0.10, 0.08, 0.05]
    assert [s["rate"]["low"] for s in a["retire_stages"]] == [0.09, 0.07, 0.05]
    assert a["inflation"] == 0.02


def test_project_linear_with_zero_rates():
    fv, series, path = project(100, 10, 24, "growth", ZERO)
    assert fv == pytest.approx(340)
    assert series == pytest.approx([100, 220, 340]) and len(path) == 25


def test_project_monthly_compounding_and_switch_to_short():
    a = assumptions_of({"inflation": 0.0})
    fv, _, _ = project(1000, 0, 12, "short", a)
    assert fv == pytest.approx(1025)  # 단기 2.5%, 월 복리 12번
    fv_low, _, _ = project(1000, 0, 12, "short", a, "low")
    assert fv_low == pytest.approx(1020)
    # 성장 목표 5년: 처음 2년은 6.5%, 남은 3년은 단기 2.5%
    fv5, _, _ = project(1000, 0, 60, "growth", a)
    assert fv5 == pytest.approx(1000 * 1.065 ** 2 * 1.025 ** 3)
    # 물가 2% 차감: 명목 2.5% → 실질 1.025/1.02
    fv_r, _, _ = project(1000, 0, 12, "short", assumptions_of())
    assert fv_r == pytest.approx(1000 * 1.025 / 1.02)
    # 월말 적립 연금 FV
    i = 1.025 ** (1 / 12) - 1
    assert project(0, 10, 12, "short", a)[0] == pytest.approx(10 * ((1 + i) ** 12 - 1) / i)


def test_retire_stages_compound():
    a = assumptions_of({"inflation": 0.0})
    fv, _, _ = project(100, 0, 31 * 12, "retire", a)
    assert fv == pytest.approx(100 * 1.10 ** 11 * 1.08 ** 10 * 1.05 ** 10)
    plan = rate_plan(31 * 12, "retire", a, __import__("datetime").date(2026, 11, 2), 34)
    assert [(p["stage"], p["months"], p["risk_ratio"]) for p in plan] == [
        ("1단계", 132, "70:30"), ("2단계", 120, "50:50"), ("3단계", 120, "30:70")]
    assert plan[0]["start"] == "2026-11" and plan[0]["end"] == "2037-11" and plan[2]["end_age"] == 65.0
    assert plan[0]["nominal"] == {"low": 9.0, "base": 10.0, "high": 11.0}


def test_low_base_high_ordering():
    a = assumptions_of()
    lo, b, hi = (project(500, 20, 200, "retire", a, sc)[0] for sc in ("low", "base", "high"))
    assert lo < b < hi


def test_required_monthly():
    assert required_monthly(100, 340, 24, "growth", ZERO) == pytest.approx(10)
    assert required_monthly(500, 340, 24, "growth", ZERO) == 0
    a = assumptions_of()
    for sc in ("low", "base", "high"):
        req = required_monthly(1000, 20000, 96, "growth", a, sc)
        assert project(1000, req, 96, "growth", a, sc)[0] == pytest.approx(20000)


def test_sequential_and_ratio_timing():
    goals = [{"start": 0, "need": 120, "months": 12, "planned_monthly": 5},
             {"start": 0, "need": 120, "months": 24, "planned_monthly": 5}]
    assert sequential(goals, 10, ZERO) == [12, 24]
    assert by_ratio(goals, ZERO) == [24, 24]
    goals2 = [{"start": 200, "need": 100, "months": 12, "planned_monthly": 0},
              {"start": 0, "need": 1e9, "months": 12, "planned_monthly": 1}]
    assert by_ratio(goals2, ZERO) == [0, None]


def test_sequential_order_by_target_not_list_order():
    goals = [{"start": 0, "need": 60, "months": 36, "planned_monthly": 0},
             {"start": 0, "need": 30, "months": 6, "planned_monthly": 0}]
    assert sequential(goals, 10, ZERO) == [9, 3]


def _a(aid, kind, man, liq, mat=None, earmark=None):
    return {"asset_id": aid, "kind": kind, "balance_won": man * 10_000, "liquidity": liq, "matures_on": mat,
            "earmark": earmark}


def test_asset_mapping_safe_first_for_short_and_invest_for_long():
    assets = [_a("cash", "입출금", 100, "즉시"), _a("dep", "정기예금", 200, "만기", "2027-03-01"),
              _a("etf", "ETF", 300, "즉시"), _a("sav", "적금", 150, "만기", "2030-01-01")]
    goals = [{"key": "short", "name": "단기", "amount": 120, "months": 24, "target": "2028-11"},
             {"key": "mid", "name": "중기", "amount": 200, "months": 60, "target": "2031-11"},
             {"key": "long", "name": "장기", "amount": 250, "months": 300, "target": "2051-11"}]
    m = asset_mapping(assets, 80, goals, [{"label": "연금저축", "balance": 500, "annual": 120, "goal": "long"}])
    rows = {r["asset_id"]: r for r in m["rows"]}
    assert rows["cash"]["assigned"] == {"emergency": 80, "short": 20}  # 비상금은 즉시 인출부터
    assert rows["dep"]["assigned"] == {"short": 100, "mid": 100}  # 단기 목표는 시점 전 만기 예금
    assert rows["etf"]["assigned"] == {"long": 250, "mid": 50}  # 투자 자산은 긴 목표부터
    assert rows["sav"]["assigned"] == {"mid": 50, "unassigned": 100}
    assert m["rebalance"] == [] and m["total"] == 750 and m["safe_total"] == 450 and m["invest_total"] == 300
    assert m["locked_rows"][0]["locked_goal"] == "long" and m["locked_total"] == 500
    assert sum(m["totals"].values()) == pytest.approx(750)
    for r in m["rows"]:
        assert sum(r["assigned"].values()) + sum(r["converted"].values()) == pytest.approx(r["balance"])


def test_asset_mapping_rebalance_when_safe_short():
    assets = [_a("cash", "입출금", 50, "즉시"), _a("etf", "ETF", 300, "즉시")]
    goals = [{"key": "short", "name": "결혼", "amount": 100, "months": 24, "target": "2028-11"},
             {"key": "long", "name": "노후", "amount": 150, "months": 300, "target": "2051-11"}]
    m = asset_mapping(assets, 80, goals)
    etf = next(r for r in m["rows"] if r["asset_id"] == "etf")
    assert etf["converted"] == {"emergency": 30, "short": 100}  # 전환 후 배정
    assert etf["assigned"] == {"long": 150, "unassigned": 20}
    assert [(x["type"], x["goal"], x["amount"]) for x in m["rebalance"]] == [("안전 전환", "emergency", 30),
                                                                             ("안전 전환", "short", 100)]
    only_safe = asset_mapping([_a("cash", "입출금", 500, "즉시")], 0, goals[1:])
    assert only_safe["rebalance"][0]["type"] == "투자 전환 검토" and only_safe["rebalance"][0]["amount"] == 150


def test_asset_mapping_unfilled_when_assets_short():
    m = asset_mapping([_a("cash", "입출금", 10, "즉시")], 30, [])
    assert m["rows"][0]["assigned"] == {"emergency": 10}
    assert m["unfilled"] == {"emergency": 20}


def test_pension_credit_limits_and_tax_cap():
    a = assumptions_of()
    pc = pension_credit(0, 700, 1200, 0.165, 100, a)
    assert pc["current_eligible"] == 600  # 연금저축 600 한도
    assert pc["current_credit"] == pytest.approx(99)
    assert pc["remaining_limit"] == 300
    assert pc["planned_eligible"] == 900  # 합산 900 한도
    assert pc["planned_credit"] == pytest.approx(148.5)
    assert pc["additional_credit"] == pytest.approx(49.5) and pc["capped_by_tax"] is False
    capped = pension_credit(0, 700, 1200, 0.165, 30, a)
    assert capped["additional_credit"] == 30 and capped["capped_by_tax"] is True
    irp = pension_credit(400, 600, None, 0.132, None, a)
    assert irp["current_eligible"] == 900 and irp["current_credit"] == pytest.approx(118.8)
    assert irp["additional_credit"] is None
    assert pension_credit(None, None, 100, None, 10, a)["current_credit"] is None


def test_annuity_payment():
    assert annuity_payment(36000, 0.0, 30) == pytest.approx(100)
    assert annuity_payment(10000, 0.045, 30) == pytest.approx(50.6685, abs=1e-3)


# ── 실제 파일 ─────────────────────────────────────────────────
KIM_CASE = ROOT / "customer" / "cases" / "kim_doyun.json"
KIM_STEP2 = ROOT / "planning" / "cases" / "kim_doyun_step2.json"
KIM_POLICY = ROOT / "output" / "policy_kim_doyun_v2.json"
CARD_KEYS = {"id", "term", "goal", "title", "action", "numbers", "basis", "evidence", "confirm", "certainty", "priority",
             "principles"}


@pytest.fixture(scope="module")
def kim():
    if not KIM_POLICY.exists():
        pytest.skip("output/policy_kim_doyun_v2.json 없음")
    step2 = json.loads(KIM_STEP2.read_text(encoding="utf-8"))
    profile = load_case(KIM_CASE)
    policy = json.loads(KIM_POLICY.read_text(encoding="utf-8"))
    reflected = reflect_build(copy.deepcopy(step2), profile, policy, "certain")
    cut = spending_cut(profile, policy, copy.deepcopy(step2))
    return reflected, profile, policy, cut


def test_kim_strategy_structure(kim):
    reflected, profile, policy, cut = kim
    before = copy.deepcopy((reflected, profile, policy, cut))
    st = build_strategy(reflected, profile, policy, cut)
    assert (reflected, profile, policy, cut) == before
    assert [g["goal"] for g in st["goals"]] == ["housing", "wedding", "child", "retire"]
    terms = {g["goal"]: g["term"] for g in st["goals"]}
    assert terms == {"housing": "중기", "wedding": "단기", "child": "장기", "retire": "장기"}
    ids = {c["id"] for c in st["cards"]}
    assert {"S1", "S2", "S3", "S4", "S5", "S6", "M1", "M2", "M3", "L1", "L2", "L3", "L4"} <= ids
    for c in st["cards"]:
        assert set(c) == CARD_KEYS
        assert c["term"] in ("단기", "중기", "장기")
        assert c["certainty"] in ("계산값", "가정 포함", "확인 필요")
        assert isinstance(c["priority"], int)
        for e in c["evidence"]:
            assert e["type"] in ("policy", "reference", "profile")
    # 배정표 합계 = M
    assert st["asset_mapping"]["total"] == pytest.approx(reflected["M"])
    assert sum(st["asset_mapping"]["totals"].values()) == pytest.approx(reflected["M"])
    # 계획 월 적립 합 = 반영 후 a_monthly × Σ비율
    a_monthly = reflected["_meta"]["deltaM_detail"]["a_monthly"]
    assert sum(g["planned_monthly"] for g in st["goals"]) == pytest.approx(a_monthly, abs=0.05)
    housing = next(g for g in st["goals"] if g["goal"] == "housing")
    assert housing["start"] == pytest.approx(housing["allocated"] + reflected["p"])
    assert housing["committed_monthly"] == pytest.approx(reflected["deltap"] / 12)
    for g in st["goals"]:
        assert g["fv_low"] <= g["fv_base"] + 1e-6 <= g["fv_high"] + 2e-6
        assert g["coverage_low"] <= g["coverage"] <= g["coverage_high"] and g["rate_plan"]
        if not g.get("ends_at_transition"):
            assert len(g["series"]) == math.ceil(g["months"] / 12) + 1
    # 세액공제: 총급여 5,760 > 5,500 → 13.2%, 연금저축 120만원 → 15.84만원
    l1 = next(c for c in st["cards"] if c["id"] == "L1")["numbers"]
    assert l1["공제율(%)"] == 13.2 and l1["현재 공제액(만원)"] == pytest.approx(15.84)
    m2 = next(c for c in st["cards"] if c["id"] == "M2")["numbers"]
    assert m2["DSR(%)"] is not None and m2["세전 연소득(만원)"] == 5760
    # D가 부채를 빼지 않았다는 경고
    assert any("빼지 않은" in w for w in st["warnings"])
    # 양육은 통계 기본값 → confirm
    l3 = next(c for c in st["cards"] if c["id"] == "L3")
    assert "전환 시점은 통계상 출산 예상 시점: 출산 계획 확인" in l3["confirm"]
    ids_guide = {p["id"] for p in st["guide"]["principles"]}
    assert all(set(c["principles"]) <= ids_guide for c in st["cards"])
    assert "P02" in next(c for c in st["cards"] if c["id"] == "S1")["principles"]
    assert {"P12", "P13"} <= set(next(c for c in st["cards"] if c["id"] == "L2")["principles"])


def test_kim_customer_mode_asset_mapping_matches_hand_check(kim):
    """김도윤 certain, 고객 목표 기준: 안전 2,700(입출금 500·정기예금 1,500·적금 700), 투자 ETF 2,000."""
    _, profile, policy, _ = kim
    step2 = json.loads(KIM_STEP2.read_text(encoding="utf-8"))
    kw = {"goal_basis": "customer"}
    reflected = reflect_build(copy.deepcopy(step2), profile, policy, "certain", **kw)
    cut = spending_cut(profile, policy, copy.deepcopy(step2), allocate_kwargs=kw)
    st = build_strategy(reflected, profile, policy, cut, **kw)
    m = st["asset_mapping"]
    assert (m["safe_total"], m["invest_total"]) == (2700, 2000)
    rows = {r["kind"]: r for r in m["rows"]}
    assert rows["ETF"]["assigned"] == {"retire": 434.55, "child": 743.7, "housing": 821.75}
    assert rows["입출금"]["assigned"] == {"emergency": 500}
    assert rows["정기예금"]["assigned"] == {"emergency": 700, "wedding": 469.99, "housing": 330.01}
    assert rows["적금"]["assigned"] == {"housing": 700}
    assert m["totals"]["housing"] == pytest.approx(1851.76) and m["totals"]["emergency"] == 1200
    assert m["rebalance"] == []
    assert m["locked_totals"] == {"housing": 300, "retire": 500}
    retire = next(g for g in st["goals"] if g["goal"] == "retire")
    assert [p["risk_ratio"] for p in retire["rate_plan"]] == ["70:30", "50:50", "30:70"]
    card = next(c for c in st["cards"] if c["goal"] == "retire" and "충족률(%)" in c["numbers"])
    assert card["numbers"]["보존자산 잔액, 연금저축·IRP·변액(만원)"] == 500 and "70:30" in card["action"]
    by = st["summary"]["monthly_need_by_rate"]
    assert by["low"] > by["base"] > by["high"]


@pytest.mark.parametrize("case", ["blank_01", "blank_02", "blank_03", "blank_04", "lee_seojun"])
def test_blank_customers_run_without_errors(case):
    path = ROOT / "customer" / "cases" / ("auto" if case.startswith("blank") else "") / f"{case}.json"
    version = "v1" if case.startswith("blank") else "v2"
    policy_path = ROOT / "output" / f"policy_{case}_{version}.json"
    if not path.exists() or not policy_path.exists():
        pytest.skip("케이스 또는 정책 결과 파일 없음")
    profile = load_case(path)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    step2 = build_step2(profile)
    reflected = reflect_build(copy.deepcopy(step2), profile, policy, "certain")
    cut = spending_cut(profile, policy, copy.deepcopy(step2))
    st = build_strategy(reflected, profile, policy, cut)
    assert st["cards"] and st["summary"]["M"] == step2["M"]
    json.dumps(st, ensure_ascii=False)  # 직렬화 가능
    if case.startswith("blank"):
        assert st["data_requests"]  # 빈칸형은 받을 자료가 있다
