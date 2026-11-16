import pytest
from conftest import offset

from spending_report.engine import analyze_rows, calculate, split_parts
from spending_report.rules import RESERVE_LABEL

KPASS = ("고정지출", "교통비", [offset("K-패스(K패스)", 24_000)])


def _run(inp):
    analysis = analyze_rows(inp)
    return analysis, calculate(inp, analysis)


def _after(calc):
    return {(b["block"], r["label"]): r["after_won"] for b in calc["cashflow"]["blocks"] for r in b["rows"]}


def _metric(calc, code):
    return next(m for m in calc["metrics"] if m["code"] == code)


def test_kim_matches_hand_calculation(make_input):
    _, calc = _run(make_input("kim_doyun", [KPASS]))
    assert calc["effect"]["policy_monthly_won"] == 24_000
    assert calc["effect"]["adjustment_monthly_won"] == 152_000  # 통신 12,000 택시 10,000 구독 10,000 배달 50,000 문화 20,000 쇼핑 50,000
    assert calc["effect"]["monthly_freed_won"] == 176_000
    after = _after(calc)
    assert after[("고정지출", "통신비")] == 73_000
    assert after[("고정지출", "교통비")] == 66_000
    assert after[("고정지출", "기타")] == 150_000
    assert after[("변동지출", "생활비")] == 650_000
    assert after[("변동지출", "문화생활비")] == 180_000
    assert after[("변동지출", "기타")] == 350_000
    assert after[("저축", RESERVE_LABEL)] == 176_000
    assert (_metric(calc, "저축률")["before"], _metric(calc, "저축률")["after"]) == (30.7, 34.8)
    assert (_metric(calc, "소비")["before"], _metric(calc, "소비")["after"]) == (64.0, 59.9)
    assert (_metric(calc, "R01")["before"], _metric(calc, "R01")["after"]) == (0, 176_000)
    assert (_metric(calc, "R02")["before"], _metric(calc, "R02")["after"]) == (11.0, 11.4)  # 2,700 ÷ 246.5, 236.9
    assert _metric(calc, "R04")["before"] == 1.1


def test_lee_uses_income_after_tax_and_keeps_mixed_row(make_input):
    analysis, calc = _run(make_input("lee_seojun", [("고정지출", "교통비", [offset("K-패스(K패스)", 18_600)])]))
    assert calc["basis"]["disposable_won"] == 11_185_500
    assert calc["effect"]["adjustment_monthly_won"] == 198_000  # 식비 183,000 문화 15,000
    after = _after(calc)
    assert after[("변동지출", "기타 생활·사업비")] == 833_300
    assert after[("고정지출", "개인 통신비")] == 50_000  # 조정 기준 금액 이하
    assert any("생활비와 업무비" in q for q in analysis.questions)
    assert _metric(calc, "저축률")["before"] == 65.6


@pytest.mark.parametrize("case", ["kim_doyun", "lee_seojun"])
def test_table_stays_balanced(make_input, case):
    _, calc = _run(make_input(case))
    totals = calc["cashflow"]["totals"]
    assert totals["income_before_won"] == totals["outflow_before_won"] == totals["outflow_after_won"]


@pytest.mark.parametrize("case", ["kim_doyun", "lee_seojun"])
def test_protected_sections_do_not_change(make_input, case):
    analysis, _ = _run(make_input(case))
    for row in analysis.rows:
        if row.source["section"] in ("저축", "보험", "세금·사회보험") or "임대료" in row.source["item"] or "대출" in row.source["item"]:
            assert row.after == row.before, row.source["item"]


def test_only_confirmed_monthly_policies_enter_the_table(make_input):
    lines = [("고정지출", "교통비", [offset("확인 필요 정책", 30_000, certainty="확인 필요")]),
             ("변동지출", "자기개발비", [offset("이용권", kind="일시금", one_off_won=[350_000, 350_000])])]
    _, calc = _run(make_input("kim_doyun", lines))
    assert calc["effect"]["policy_monthly_won"] == 0


def test_same_line_uses_largest_policy_only(make_input):
    lines = [("고정지출", "교통비", [offset("작은 정책", 7_000), offset("큰 정책", 24_000)])]
    analysis, calc = _run(make_input("kim_doyun", lines))
    assert calc["effect"]["policy_monthly_won"] == 24_000
    assert [r["target"] for r in analysis.recommendations if r["kind"] == "정책 혜택"] == ["큰 정책"]


def test_keep_request_stays_on_its_own_item(make_input):
    analysis, _ = _run(make_input("kim_doyun"))
    necessity = {c["label"]: c["necessity"] for c in analysis.classification}
    assert necessity["의료·약제"] == "필수"
    assert necessity["쇼핑"] == "선택"
    assert necessity["연간 비정기 지출 적립"] == "미확인"
    assert any(r["target"] == "쇼핑" for r in analysis.recommendations)


def test_customer_keep_request_blocks_reduction(make_input):
    def keep_shopping(profile):
        row = next(r for r in profile["cashflow_items"] if r["detail"] and "쇼핑" in r["detail"])
        row["detail"] = "쇼핑 25 + 의료·약제 5 + 연간 비정기 지출 적립 10(만원). 쇼핑 유지 희망"
    analysis, _ = _run(make_input("kim_doyun", mutate=keep_shopping))
    assert not any(r["target"] == "쇼핑" for r in analysis.recommendations)


def test_youth_rate_drop_is_in_annual_effect(make_input):
    _, calc = _run(make_input("kim_doyun", [KPASS]))
    # 2026-11-02 기준 5개월 24,000원 + 7개월 16,000원, 소비 조정 152,000원 × 12
    assert calc["effect"]["annual_freed_won"] == 232_000 + 1_824_000
    assert "2027-04-18" in calc["effect"]["annual_note"]


def test_customer_past_youth_age_gets_general_rate(make_input):
    def older(profile):
        profile["facts"]["birth_date"]["value"] = "1985-01-01"
    analysis, calc = _run(make_input("kim_doyun", [KPASS], mutate=older))
    assert calc["effect"]["policy_monthly_won"] == 16_000
    assert analysis.rows and calc["effect"]["annual_note"] == ""


def test_recommendation_follows_result_contract(make_input):
    analysis, _ = _run(make_input("kim_doyun", [KPASS]))
    rec = next(r for r in analysis.recommendations if r["target"] == "배달·외식")
    assert rec["amount"] == {"before_won": 200_000, "after_won": 150_000, "monthly_change_won": -50_000}
    assert rec["customer_choice"] == "미선택"
    assert rec["period"] == "월" and rec["assumptions"] and rec["alternatives"][0]["label"] == "유지"


def test_split_parts():
    assert split_parts("교통비", 100_000, "대중교통 8 + 택시 2(만원)") == [("대중교통", 80_000), ("택시", 20_000)]
    assert split_parts("교통비", 90_000, "대중교통 8 + 택시 2(만원)") == [("교통비", 90_000)]  # 합계 불일치
    assert split_parts("저축", 7_340_200, "2026 월평균 예산상 잔여액") == [("저축", 7_340_200)]
    assert split_parts("보험", 30_000, None) == [("보험", 30_000)]
