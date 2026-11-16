"""고객 재무목표 기준 계획(planning.goals, allocate(goal_basis='customer')) 시험."""
import copy
import json
from pathlib import Path

import pytest

from customer.profile import load_case
from planning.allocation import allocate
from planning.goals import category_of, classify_goals, has_children

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "customer" / "cases"
KIM_STEP2 = json.loads((ROOT / "planning" / "cases" / "kim_doyun_step2.json").read_text(encoding="utf-8"))


def case(name):
    path = CASES / ("auto" if name.startswith("blank") else "") / f"{name}.json"
    if not path.exists():
        pytest.skip(f"{name} 없음")
    return load_case(path)


@pytest.mark.parametrize("name,expect", [
    ("대출 상환", "debt"), ("교육자금", "child"), ("생활예비금", "emergency"), ("가족예비금", "emergency"),
    ("비상금", "emergency"), ("결혼자금", "wedding"), ("주거자금", "housing"), ("은퇴 생활비", "retire"),
    ("서울 2룸 이상 자가 마련", "housing"), ("이사 보증금 순증", "other"), ("소득중단 대비 예비금", "emergency"),
    ("1년에 한 번 해외여행", "other")])
def test_category_of(name, expect):
    assert category_of(name) == expect


def test_classify_real_customers():
    got = {}
    for name in ("kim_doyun", "blank_01", "blank_02", "blank_03", "blank_04", "lee_seojun"):
        p = case(name)
        got[name] = [(g["category"], g["term_stated"], g["amount"]) for g in classify_goals(p, p["as_of_date"], 30)]
    assert ("debt", "단기", 1500.0) in got["blank_02"]
    assert ("child", "중기", 3000.0) in got["blank_04"] and ("emergency", "단기", 2000.0) in got["blank_04"]
    assert ("emergency", "단기", 3000.0) in got["blank_03"]
    assert ("wedding", "중기", None) in got["blank_01"]  # 금액 미확인 그대로
    assert ("retire", "장기", 200.0) in got["kim_doyun"] and ("other", "단기", 2000.0) in got["lee_seojun"]


def test_default_allocate_unchanged():
    p = case("kim_doyun")
    a = allocate(copy.deepcopy(KIM_STEP2), p)
    b = allocate(copy.deepcopy(KIM_STEP2), p, goal_basis="statistical")
    assert a == b
    assert "goal_basis" not in a and "goals_input" not in a and "source" not in a["emergency"]
    assert [g["goal"] for g in a["goals"]] == ["housing", "wedding", "child", "retire"]
    with pytest.raises(ValueError):
        allocate(KIM_STEP2, p, goal_basis="x")


def test_kim_customer_basis_matches_hand_check():
    p = case("kim_doyun")
    a = allocate(copy.deepcopy(KIM_STEP2), p, goal_basis="customer")
    assert a["emergency"]["amount"] == 1200 and a["emergency"]["source"] == "고객 목표"
    assert a["emergency"]["income_rule_amount"] == 642 and a["distributable"] == 3500
    g = {x["goal"]: x for x in a["goals"]}
    assert set(g) == {"housing", "wedding", "child", "retire"} and all(x["kind"] == "base" for x in g.values())
    assert (g["wedding"]["need"], g["wedding"]["years"]) == (3000, 3.0)
    assert (g["housing"]["need"], g["housing"]["existing"], g["housing"]["gap"], g["housing"]["years"]) == (20000, 300, 19700, 5.0)
    assert g["retire"]["need"] == pytest.approx((200 - 70.0427) * 12 * 18.7, abs=0.05) and g["retire"]["existing"] == 500
    assert g["child"]["need_source"] == "통계 기본값" and g["child"]["need"] == pytest.approx(19621.57)
    assert [g[k]["ratio"] for k in ("wedding", "housing", "retire", "child")] == [13.4, 52.9, 12.4, 21.3]
    assert sum(x["ratio"] for x in a["goals"]) == pytest.approx(100.0)
    assert sum(x["amount"] for x in a["goals"]) == pytest.approx(3500, abs=0.01)
    assert g["housing"]["reference_need"] == pytest.approx(16596.69) and g["housing"]["need_diff"] == pytest.approx(3403.31)
    assert [u["goal"] for u in a["unstated_goals"]] == ["child"]


def test_statistical_fallback_and_debt_extra():
    p = case("blank_02")
    s2 = {"p": 0, "q": 700, "r": 0, "s": 0, "M": 3800, "deltaM": 2292,
          "_meta": {"deltaM_detail": {"income_monthly": 440}, "asset_detail": {"M_items": ["ISA 1500", "ETF 2300"]}}}
    a = allocate(s2, p, goal_basis="customer")
    g = {x["goal"]: x for x in a["goals"]}
    assert g["wedding"]["need_source"] == "통계 대체" and g["wedding"]["kind"] == "base"
    assert g["debt"]["kind"] == "extra" and g["debt"]["need"] == 1500 and g["debt"]["ratio"] > 0
    assert a["emergency"]["source"] == "월 소득 × 배수"  # 비상금 목표를 적지 않음
    assert any("결혼" in r["item"] for r in a["data_requests"])


def test_children_present_excludes_child_and_education_is_extra():
    p = case("blank_04")
    assert has_children(p) is True
    s2 = {"p": 500, "q": 0, "r": 0, "s": 0, "M": 1200, "deltaM": 180,
          "_meta": {"deltaM_detail": {"income_monthly": 290}, "asset_detail": {"M_items": ["입출금 1200"]}}}
    a = allocate(s2, p, goal_basis="customer")
    g = {x["goal"]: x for x in a["goals"]}
    assert "housing" not in g and "wedding" not in g  # 주택 보유·기혼 제외 규칙 유지
    assert g["child"]["kind"] == "extra" and g["child"]["name"] == "교육자금" and g["child"]["need"] == 3000
    assert not any(x["kind"] == "base" and x["category"] == "child" for x in a["goals"])  # 양육은 처음부터 제외
    assert a["children_present"] is True and "현재 지출" in a["child_note"]
    assert a["emergency"]["amount"] == 1200 and a["emergency"]["need"] == 2000 and a["emergency"]["capped"]


def test_education_without_children_is_extra():
    p = case("blank_04")
    p = copy.deepcopy(p)
    p["facts"]["household_size"]["value"] = 1
    p["facts"]["marital_status"]["value"] = "미혼"
    s2 = {"p": 0, "q": 0, "r": 0, "s": 0, "M": 5000, "deltaM": 1000,
          "_meta": {"deltaM_detail": {"income_monthly": 290}, "asset_detail": {"M_items": ["입출금 5000"]}}}
    a = allocate(s2, p, goal_basis="customer")
    g = {x["goal"]: x for x in a["goals"]}
    assert g["child"]["need_source"] == "통계 기본값"  # 양육 기본 목표는 통계값
    edu = [x for x in a["goals"] if x["kind"] == "extra" and x["category"] == "child"]
    assert len(edu) == 1 and edu[0]["need"] == 3000 and edu[0]["name"] == "교육자금"


def kim_with_trip():
    """FF 2번에 고객이 '1년에 한 번 해외여행 300만원'을 적었다고 가정한 김도윤 사본."""
    p = copy.deepcopy(case("kim_doyun"))
    p["goals"].append({"goal_id": "g-x", "term": "단기", "name": "1년에 한 번 해외여행", "due": None,
                       "amount_won": 3_000_000, "amount_basis": "일시금", "status": "기재", "raw": None})
    return p


def test_recurring_goal_from_profile():
    p = kim_with_trip()
    items = classify_goals(p, p["as_of_date"], 34)
    assert items[-1]["recurrence"] == "annual" and items[-1]["category"] == "other"
    a = allocate(copy.deepcopy(KIM_STEP2), p, goal_basis="customer")
    trip = next(x for x in a["goals"] if x["kind"] == "extra")
    assert trip["recurrence"] == "annual" and trip["years"] == 1 and trip["recurring_monthly"] == 25
    assert trip["weight"] == 300 and trip["name"] == "기타 목표 1"
    assert {x["goal"] for x in a["goals"] if x["kind"] == "base"} == {"housing", "wedding", "child", "retire"}
    assert sum(x["ratio"] for x in a["goals"]) == pytest.approx(100.0)
    item = next(i for i in a["goals_input"] if i["goal_id"] == "g-x")
    assert item["plan_use"] == "추가 목표로 분배(매년 반복)"


def test_no_extra_goal_without_customer_text():
    a = allocate(copy.deepcopy(KIM_STEP2), case("kim_doyun"), goal_basis="customer")
    assert all(x["kind"] == "base" for x in a["goals"])
    assert all(i.get("source") == "고객 Fact Finding" for i in a["goals_input"])


def test_strategy_monthly_need_includes_recurring():
    from planning.reflect import build
    from planning.spending_cut import spending_cut
    from planning.strategy import build_strategy
    policy_path = ROOT / "output" / "policy_kim_doyun_v2.json"
    if not policy_path.exists():
        pytest.skip("정책 결과 없음")
    p = kim_with_trip()
    pol = json.loads(policy_path.read_text(encoding="utf-8"))
    kw = {"goal_basis": "customer"}
    ref = build(copy.deepcopy(KIM_STEP2), p, pol, "certain", **kw)
    st = build_strategy(ref, p, pol, spending_cut(p, pol, copy.deepcopy(KIM_STEP2), allocate_kwargs=kw), **kw)
    trip = next(g for g in st["goals"] if g["kind"] == "extra")
    need = sum(max(g["required_monthly"], g.get("recurring_monthly") or 0) for g in st["goals"]
               if not g.get("ends_at_transition"))
    assert st["summary"]["monthly_need"] == pytest.approx(need, abs=0.01)
    assert trip["recurring_monthly"] == 25
    card = next(c for c in st["cards"] if c["goal"] == trip["goal"])
    assert card["numbers"]["매년 반복 월 적립(만원)"] == 25 and "매년 반복" in card["action"]
    assert card["id"][0] == "S" and not card["id"].startswith("G")
    assert any("기본 목표(고객 미기재" in d["topic"] for d in st["decisions"])


def test_from_documents_keeps_raw():
    from customer.from_documents import build_case
    folder = ROOT / "가상고객데이터" / "빈칸형_가상고객_4명" / "02_박준호"
    if not folder.exists():
        pytest.skip("빈칸형 문서 없음")
    goals = build_case("doc_test", folder)["goals"]
    raw = {g["name"]: g.get("raw") for g in goals}
    assert raw["대출 상환"] == "28.11 대출 1,500만원 상환"
    debt = next(g for g in goals if g["name"] == "대출 상환")
    assert debt["due"] == "2028-11" and debt["amount_won"] == 15_000_000
    retire = next(g for g in goals if g["name"] == "은퇴 생활비")
    assert retire["due"] == "65세" and retire["amount_won"] == 2_500_000 and retire["raw"] == "65세부터 월 250만원"
    housing = next(g for g in goals if g["name"] == "주거자금")
    assert housing["amount_won"] == 250_000_000  # '2.5억원'
