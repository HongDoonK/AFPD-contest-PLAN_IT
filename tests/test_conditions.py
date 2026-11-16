from policy_enrich.amounts import parse_amount
from policy_enrich.conditions import build_condition, dedup_key, recruit_status

TODAY = "20261104"


def _row(**kw):
    base = {"policy_id": "P1", "policy_name": "테스트", "income_type": "0043003",
            "income_min": "0", "income_max": "0", "income_etc": None,
            "apply_condition": None, "support_content": None,
            "apply_period_type": "0057002", "apply_period": None, "biz_end_date": None}
    base.update(kw)
    return base


def test_parse_amount_variants():
    assert parse_amount("6천만원") == 60_000_000
    assert parse_amount("1억원") == 100_000_000
    assert parse_amount("3,500만원") == 35_000_000
    assert parse_amount("3,847천원") == 3_847_000
    assert parse_amount("1억 2천만원") == 120_000_000
    assert parse_amount("138,780원") == 138_780
    assert parse_amount("1200") is None


def test_personal_and_couple_annual_income():
    c = build_condition(_row(income_etc="본인 연소득 6천만원 이하, 부부합산 1억원 이하"), TODAY)
    assert c.annual_income_max_won == 60_000_000
    assert c.couple_annual_income_max_won == 100_000_000
    assert c.income_basis == "본인"
    assert c.parse_confidence == "high"


def test_personal_vs_household_median():
    c = build_condition(_row(income_etc="청년 기준중위소득 60%(월소득 154만원) 이하, "
                                        "원가구 기준중위소득 100%(월 536만원) 이하"), TODAY)
    assert c.personal_median_pct_max == 60
    assert c.household_median_pct_max == 100
    assert c.income_basis == "본인+가구"


def test_independent_household_is_personal():
    c = build_condition(_row(income_etc="(청년독립가구) 기준 중위소득 60%이하 (원가구) 기준 중위소득 100% 이하"), TODAY)
    assert c.personal_median_pct_max == 60
    assert c.household_median_pct_max == 100


def test_youth_tier_is_kept_apart():
    c = build_condition(_row(income_etc="청년:연소득 5천만원 이하, 청년 외:연소득 6천만원 이하, 신혼부부:연소득 7.5천만원 이하"), TODAY)
    assert c.youth_annual_income_max_won == 50_000_000
    assert c.annual_income_max_won == 75_000_000
    c2 = build_condition(_row(income_etc="연소득 ➊(청년) 5천만원, ➋(청년 외) 6천만원 이하"), TODAY)
    assert c2.youth_annual_income_max_won == 50_000_000
    assert c2.income_basis == "본인" and c2.parse_confidence == "high"
    c3 = build_condition(_row(income_etc="연소득 (청년) 5천만원 (청년 외)6천만원 (신혼부부)7.5천만원 이하"), TODAY)
    assert c3.youth_annual_income_max_won == 50_000_000


def test_couple_income_range_takes_upper():
    c = build_condition(_row(income_etc="자녀 여부에 따라 부부합산 연 8,000만원~9,800만원"), TODAY)
    assert c.couple_annual_income_max_won == 98_000_000


def test_median_with_health_insurance():
    c = build_condition(_row(income_etc="기준중위소득 150%이하(월 소득 3,847천원 이하) / 건강보험료로 판단 : "
                                        "직장가입자 138,780원 지역가입자 68,641원"), TODAY)
    assert c.household_median_pct_max == 150
    assert c.monthly_income_max_won == 3_847_000
    assert c.health_insurance_max_won == 138_780


def test_tier_lower_bound_is_ignored_and_fallback_to_support_text():
    # income_type이 '무관'으로 잘못 들어간 청년내일저축계좌류: 지원내용에서 찾는다
    c = build_condition(_row(income_type="0043001", support_content=
                             "○ 중위소득 50%초과 ~ 100%이하: 10만원 정액 매칭\n○ 중위소득 50%이하: 30만원 정액 매칭"), TODAY)
    assert c.household_median_pct_max == 100
    assert c.parse_confidence == "partial"
    assert c.income_basis == "가구"


def test_api_numeric_income():
    c = build_condition(_row(income_type="0043002", income_max="6000"), TODAY)
    assert c.annual_income_max_won == 60_000_000


def test_unrestricted_income():
    c = build_condition(_row(income_type="0043001", support_content="월 20만원 지원"), TODAY)
    assert c.income_basis == "무관"
    assert c.parse_confidence == "not_applicable"


def test_recruit_status():
    assert recruit_status("0057003", None, None, TODAY)[0] == "closed"
    assert recruit_status("0057002", None, None, TODAY)[0] == "always"
    assert recruit_status("0057002", None, "20251231", TODAY)[0] == "closed"
    assert recruit_status("0057001", "20260101 ~ 20261231", None, TODAY) == ("open", "20261231")
    assert recruit_status("0057001", "20250101 ~ 20250131", None, TODAY)[0] == "closed"


def test_dedup_key():
    assert dedup_key("2026년 청년 희망디딤돌 통장사업 대상자 모집 공고") == dedup_key("청년 희망디딤돌 통장사업 대상자")
    assert dedup_key("‘빛나는 제주 청년희망 대출’ 이차보전") == "빛나는제주청년희망대출이차보전"
