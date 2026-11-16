"""planning.allocation 시험. 웹 값에 의존하지 않도록 고정된 가짜 기준값을 쓴다."""
import copy

import pytest

from planning.allocation import allocate, largest_remainder
from planning.reference import REFERENCE


def _e(value, kind="통계", year=2026):
    return {"value": value, "unit": "x", "year": year, "source": "테스트", "url": None,
            "verified_at": "2026-11-12", "kind": kind, "check": "가정", "note": None}


FAKE_REF = {
    "first_marriage_age": _e({"남": 34.0, "여": 32.0}),
    "wedding_cost_ex_housing": _e(6000),
    "wedding_cost_low": _e(2000),
    "wedding_cost_high": _e(None),
    "child_cost_to_college": _e(30000, year=2012),
    "cpi_annual": _e({"2012": 100.0, "2025": 120.0}),
    "child_spend_years": _e(22, "가정"),
    "first_child_parent_age": _e({"남": 36.0, "여": 33.0}),
    "house_price_sido": _e({"서울": 100000, "경기": 50000, "부산": 200000}),
    "house_price_national": _e(40000),
    "mortgage_cap": _e(60000),
    "mortgage_cap_regions": _e(["서울", "경기", "인천"], "가정"),
    "first_home_age": _e(40.0),
    "ltv": _e(0.7, "가정"),
    "purchase_cost_rate": _e(0.03, "가정"),
    "retire_min_living_single": _e(140.0),
    "retire_adequate_living_single": _e(200.0),
    "nps_old_age_avg_monthly": _e(70.0),
    "life_expectancy": _e({"남": 80.0, "여": 86.0, "전체": 83.0}),
    "life_expectancy_at_60": _e({"남": 24.0, "여": 28.0}),
    "pension_start_age": _e(65, "가정"),
    "retire_age": _e(60, "가정"),
    "wedding_self_share": _e(0.5, "가정"),
    "child_self_share": _e(0.5, "가정"),
    "default_children": _e(1, "가정"),
    "emergency_multiple": _e(1.5, "가정"),
    "emergency_multiple_low": _e(1.0, "가정"),
}

STEP2 = {
    "p": 300.0, "p_rate": 2.8, "p_date": "2024-05-01", "deltap": 120.0,
    "q": 0.0, "q_rate": None, "q_date": None, "deltaq": None,
    "r": 500.0, "r_rate": None, "r_date": "2024-01-01", "deltar": 120.0,
    "s": 0.0, "s_rate": None, "s_date": None, "deltas": None,
    "M": 4700.0, "deltaM": 1800.0,
    "_meta": {"deltaM_detail": {"income_monthly": 400.0},
              "asset_detail": {"M_items": ["입출금 500", "정기예금 1500", "자유적금 700", "ETF 2000"]}},
}


def profile(goals=(), **facts):
    base = {"birth_date": "1992-04-18", "sex": "남", "region_sido": "경기", "marital_status": "미혼",
            "home_owner": False}
    base.update(facts)
    return {"case_id": "t", "profile_version": "v1", "as_of_date": "2026-11-12",
            "facts": {k: {"value": v} for k, v in base.items()}, "goals": list(goals)}


def run(step2=STEP2, prof=None, **kw):
    return allocate(copy.deepcopy(step2), prof or profile(), FAKE_REF, **kw)


def goal(res, key):
    return next(g for g in res["goals"] if g["goal"] == key)


# ① 두 가지 M 해석
def test_m_interpretations():
    a, b = run(), run(m_includes_locked=True)
    assert a["m_avail"]["value"] == 4700 and b["m_avail"]["value"] == 3900
    for r in (a, b):
        assert r["m_avail"]["if_m_excludes_locked"] == 4700 and r["m_avail"]["if_m_includes_locked"] == 3900
    assert a["m_avail"]["M_items_sum"] == 4700 and not any("M_items" in w for w in a["warnings"])


def test_m_items_mismatch_warns():
    s = copy.deepcopy(STEP2)
    s["_meta"]["asset_detail"]["M_items"].append("기타 100")
    assert any("M_items 합" in w for w in run(s)["warnings"])


# ② 비상금이 M_avail을 넘는 경우
def test_emergency_capped_at_m_avail():
    s = copy.deepcopy(STEP2)
    s["M"] = 300.0
    s["_meta"]["asset_detail"]["M_items"] = ["입출금 300"]
    res = run(s)
    assert res["emergency"]["need"] == 600 and res["emergency"]["amount"] == 300
    assert res["emergency"]["capped"] and res["distributable"] == 0
    assert all(g["amount"] == 0 for g in res["goals"] if g["active"])


def test_emergency_range():
    assert run()["emergency"]["range"] == [400.0, 600.0]


# ③ 비율 합 100.0, 금액 합 D
@pytest.mark.parametrize("m", [4700.0, 3333.3, 1.0, 12345.67])
def test_ratio_and_amount_sums(m):
    s = copy.deepcopy(STEP2)
    s["M"] = m
    s["_meta"]["asset_detail"]["M_items"] = [f"현금 {m}"]
    res = run(s)
    act = [g for g in res["goals"] if g["active"]]
    assert round(sum(g["ratio"] for g in act), 1) == 100.0
    assert round(sum(g["ratio_by_need"] for g in act), 1) == 100.0
    assert round(sum(g["amount"] for g in act), 2) == round(res["distributable"], 2)


def test_largest_remainder_exact():
    out = largest_remainder({"a": 1, "b": 1, "c": 1}, 1000)
    assert sum(out.values()) == 1000 and sorted(out.values()) == [333, 333, 334]


def test_goal_values_with_fake_reference():
    res = run()
    h, w, c, r = (goal(res, k) for k in ("housing", "wedding", "child", "retire"))
    assert h["need"] == pytest.approx(50000 * 0.33) and h["gap"] == pytest.approx(50000 * 0.33 - 300)
    assert h["years"] == 6  # 40 − 34
    assert w["need"] == 3000 and w["years"] == 1 and w["range"] == [1000.0, 3000.0]
    assert c["need"] == pytest.approx(30000 * 1.2 * 0.5) and c["years"] == 13  # (36−34) + 22/2
    # 60~65세 생활비 전액 + 65~84세(60+24) 생활비−연금
    assert r["need"] == pytest.approx(140 * 12 * 5 + 70 * 12 * 19) and r["existing"] == 500
    assert r["range"][1] == pytest.approx(200 * 12 * 5 + 130 * 12 * 19)
    assert r["years"] == 26
    assert any("결혼" in x and "years=1" in x for x in res["warnings"])
    assert all(g["horizon_source"]["source"] == "통계 연령" for g in res["goals"])


def test_unknown_sido_falls_back_to_national():
    res = run(prof=profile(region_sido="제주"))
    assert goal(res, "housing")["inputs"]["house_price"] == 40000
    assert any("전국값 대체" in x for x in res["warnings"])


# v2: 목표 시점은 고객 목표 기한 우선
KIM_LIKE_GOALS = [
    {"goal_id": "g-emergency", "name": "비상금", "due": "2027-11"},
    {"goal_id": "g-wedding", "name": "결혼자금(본인 부담 희망액)", "due": "2029-11"},
    {"goal_id": "g-housing", "name": "주거자금", "due": "2031-11"},
    {"goal_id": "g-retire", "name": "은퇴 생활비", "due": "65세"},
]


def test_customer_goal_due_overrides_statistics():
    res = run(prof=profile(goals=KIM_LIKE_GOALS))
    h, w, c, r = (goal(res, k) for k in ("housing", "wedding", "child", "retire"))
    assert h["years"] == 5.0 and h["horizon_source"]["goal_id"] == "g-housing"
    assert w["years"] == 3.0 and w["horizon_source"]["source"] == "고객 목표"
    assert r["years"] == 31 and r["target_age"] == 65 and r["horizon_source"]["due"] == "65세"
    assert c["horizon_source"]["source"] == "통계 연령"


def test_retire_age_follows_customer_goal_when_not_given():
    r = goal(res := run(prof=profile(goals=KIM_LIKE_GOALS)), "retire")
    assert r["inputs"]["retire_age"] == 65 and r["inputs"]["retire_age_source"].startswith("고객 목표 g-retire")
    assert r["inputs"]["years_before_pension"] == 0
    assert r["need"] == pytest.approx(70 * 12 * (84 - 65))
    assert not any("은퇴 연령" in x for x in res["warnings"])


def test_explicit_retire_age_wins_and_warns():
    res = run(prof=profile(goals=KIM_LIKE_GOALS), retire_age=60)
    r = goal(res, "retire")
    assert r["inputs"]["retire_age"] == 60 and r["inputs"]["retire_age_source"] == "인자"
    assert r["years"] == 31  # 시점은 여전히 고객 목표
    assert r["need"] == pytest.approx(140 * 12 * 5 + 70 * 12 * 19)
    assert any("은퇴 연령 60세(인자)" in x for x in res["warnings"])


def test_retire_goal_date_due_keeps_default_age():
    res = run(prof=profile(goals=[{"goal_id": "r", "name": "노후 준비", "due": "2050-01"}]))
    r = goal(res, "retire")
    assert r["inputs"]["retire_age"] == 60 and r["inputs"]["retire_age_source"] == "기준값 표 기본값"
    assert any("기준값 표 기본값" in x for x in res["warnings"])


def test_customer_goal_date_and_unreadable_due():
    res = run(prof=profile(goals=[{"goal_id": "w", "name": "결혼", "due": "2028-11-12"},
                                  {"goal_id": "h", "name": "내 집 마련", "due": "미정"}]))
    assert goal(res, "wedding")["years"] == pytest.approx(2.0, abs=0.01)
    assert goal(res, "housing")["horizon_source"]["source"] == "통계 연령"
    assert any("읽지 못함" in x for x in res["warnings"])


def test_customer_goal_less_than_one_year_floors():
    res = run(prof=profile(goals=[{"goal_id": "w", "name": "결혼", "due": "2027-03"}]))
    assert goal(res, "wedding")["years"] == 1


# v2: 양육 years = 출산까지 + 지출기간/2
def test_child_with_existing_children():
    c = goal(run(children=2), "child")
    assert c["years"] == 11 and c["inputs"]["years_to_birth"] == 0
    assert c["need"] == pytest.approx(30000 * 1.2 * 0.5 * 2)


def test_child_birth_age_passed():
    res = run(prof=profile(birth_date="1980-01-01"))  # 46세 > 36세
    assert goal(res, "child")["years"] == 11
    assert any("이미 지남" in x for x in res["warnings"])


def test_cpi_missing_uses_factor_one():
    ref = copy.deepcopy(FAKE_REF)
    ref["cpi_annual"]["value"] = {"2025": 120.0}
    res = allocate(copy.deepcopy(STEP2), profile(), ref)
    assert goal(res, "child")["need"] == 15000
    assert any("물가 계수 1.0" in x for x in res["warnings"])


# v2: 노후는 연금 개시 전후를 나눔
def test_retire_age_after_pension_start():
    r = goal(run(retire_age=70), "retire")
    assert r["inputs"]["years_before_pension"] == 0
    assert r["need"] == pytest.approx(70 * 12 * (84 - 70))


def test_life_expectancy_at_60_missing_falls_back():
    ref = copy.deepcopy(FAKE_REF)
    ref["life_expectancy_at_60"]["value"] = None
    res = allocate(copy.deepcopy(STEP2), profile(), ref)
    assert goal(res, "retire")["inputs"]["life_end_age"] == 80.0
    assert any("60세 기대여명 없음" in x for x in res["warnings"])


# v2: 수도권만 대출 상한
def test_mortgage_cap_only_in_capital_area():
    seoul = goal(run(prof=profile(region_sido="서울")), "housing")
    assert seoul["inputs"]["loan"] == 60000 and seoul["need"] == pytest.approx(100000 - 60000 + 3000)
    busan = goal(run(prof=profile(region_sido="부산")), "housing")
    assert busan["inputs"]["loan_cap"] is None and busan["inputs"]["loan"] == pytest.approx(140000)


# ④ 기혼·주택 보유 시 목표 제외
def test_married_and_homeowner_excluded():
    res = run(prof=profile(marital_status="기혼", home_owner=True))
    assert not goal(res, "housing")["active"] and not goal(res, "wedding")["active"]
    assert goal(res, "housing")["ratio"] is None and goal(res, "wedding")["amount"] is None
    act = [g for g in res["goals"] if g["active"]]
    assert {g["goal"] for g in act} == {"child", "retire"}
    assert round(sum(g["ratio"] for g in act), 1) == 100.0


# ⑤ null 금리·가입일
def test_null_rate_and_date_stay_null():
    rows = {r["key"]: r for r in run()["locked_assets"]}
    assert rows["q"]["rate_pct"] is None and rows["q"]["opened_on"] is None and rows["q"]["elapsed_years"] is None
    assert rows["r"]["rate_pct"] is None and rows["r"]["elapsed_years"] == pytest.approx(2.86, abs=0.01)
    assert rows["p"]["annual_contribution"] == 120.0 and rows["s"]["annual_contribution"] is None


# ⑥ active 목표 없음
def test_no_active_goal_gives_reason():
    ref = copy.deepcopy(FAKE_REF)
    ref["child_cost_to_college"]["value"] = None
    ref["nps_old_age_avg_monthly"]["value"] = None
    res = allocate(copy.deepcopy(STEP2), profile(marital_status="기혼", home_owner=True), ref)
    assert res["no_ratio_reason"] == "active 목표 없음"
    assert all(g["ratio"] is None for g in res["goals"])


def test_zero_weight_gives_reason():
    s = copy.deepcopy(STEP2)
    s["r"] = 1e9  # 노후 부족액 0
    ref = copy.deepcopy(FAKE_REF)
    ref["child_cost_to_college"]["value"] = 0
    res = allocate(s, profile(marital_status="기혼", home_owner=True), ref)
    assert "Σweight = 0" in res["no_ratio_reason"]


# ⑦ 달성 가능성 판정
def test_feasibility_flags():
    res = run()
    f = res["feasibility"]
    assert f["monthly_surplus"] == 150.0
    assert f["monthly_needed_total"] == pytest.approx(sum(g["monthly_needed"] for g in res["goals"] if g["active"]), abs=0.02)
    assert f["feasible"] is False and f["shortfall_monthly"] > 0
    for g in res["goals"]:
        assert g["monthly_needed"] == pytest.approx((g["gap"] - g["amount"]) / (g["years"] * 12), abs=0.01)
        assert g["coverage"] == pytest.approx(g["amount"] / g["gap"], abs=1e-4)


def test_feasible_when_surplus_large():
    s = copy.deepcopy(STEP2)
    s["deltaM"] = 1e7
    f = run(s)["feasibility"]
    assert f["feasible"] is True and f["shortfall_monthly"] == 0


# ⑧ 실제 기준값 표: 모든 항목에 출처·확인일
@pytest.mark.parametrize("key", sorted(REFERENCE))
def test_reference_entries_have_source(key):
    e = REFERENCE[key]
    assert e["source"] and e["verified_at"] == "2026-11-12"
    assert e["kind"] in ("통계", "가정")
    assert e["check"] in ("원문 확인", "기사 확인", "가정", "못 찾음")
    if e["check"] == "못 찾음":
        assert e["value"] is None
    if e["kind"] == "통계" and e["check"] != "못 찾음":
        assert e["url"]


def test_fake_reference_has_same_keys():
    assert set(FAKE_REF) == set(REFERENCE)


def test_reference_used_lists_sources():
    res = run()
    assert "first_marriage_age" in res["reference_used"] and "ltv" in res["reference_used"]
