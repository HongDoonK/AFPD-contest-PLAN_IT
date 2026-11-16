from customer.policy_match import MET, NOT_MET, UNKNOWN, judge

MEDIAN = {1: 2_564_238, 2: 4_199_292}


def _customer(**kw):
    base = {"case_id": "t", "profile_version": "v1", "as_of": "2026-11-02", "customer_type": "근로",
            "age": 34, "region_code": "41133", "sex": "남", "household_size": 1, "marital_status": "미혼",
            "home_owner": False, "employment_status": "재직", "employer_size": None, "parents_household": "별도 거주",
            "income": {("근로", "총급여"): {"amount_won": 57_600_000, "tax_year": 2025, "status": "확인"}},
            "net_assets_won": 100_000_000, "goals": "비상금 결혼자금 주거자금", "facts": {}}
    base.update(kw)
    return base


def _policy(**kw):
    base = {"policy_id": "P", "policy_name": "테스트 정책", "institution": "기관", "category_main": "주거",
            "category_sub": "", "min_age": 19, "max_age": 39, "job_cd": "0013010", "school_cd": "0049010",
            "special_cd": "0014010", "marriage_status": "0055003", "search_text": "", "income_text": "",
            "income_basis": "무관", "condition_confidence": "not_applicable",
            "personal_median_pct_max": None, "household_median_pct_max": None, "annual_income_max_won": None,
            "youth_annual_income_max_won": None, "monthly_income_max_won": None,
            "couple_annual_income_max_won": None, "asset_max_won": None,
            "benefit_type": "현금지원", "summary_line": "", "monthly_amount_won": None, "duration_months": None,
            "total_amount_won": None, "amount_min_won": None, "amount_max_won": None, "loan_limit_won": None,
            "interest_support_pct": None, "discount_pct": None, "discount_won": None, "match_ratio": None, "own_saving_monthly_won": None,
            "benefit_confidence": "none", "recruit_status": "always", "apply_end_date": None,
            "application_url": "", "ref_url1": "", "dedup_key": "k"}
    base.update(kw)
    return base


def _result(finding, name):
    return next(x["result"] for x in finding["conditions"] if x["condition"] == name)


def test_plain_policy_is_met():
    assert judge(_policy(), _customer(), MEDIAN)["verdict"] == "입력상 충족"


def test_age_over_limit():
    f = judge(_policy(max_age=34), _customer(age=35), MEDIAN)
    assert f["verdict"] == "불충족" and _result(f, "연령") == NOT_MET


def test_youth_tier_income_is_used():
    p = _policy(income_basis="본인", condition_confidence="high",
                annual_income_max_won=75_000_000, youth_annual_income_max_won=50_000_000)
    assert _result(judge(p, _customer(), MEDIAN), "본인 연소득(청년 기준)") == NOT_MET


def test_household_median_uses_household_size():
    p = _policy(income_basis="가구", condition_confidence="high", household_median_pct_max=150)
    f = judge(p, _customer(), MEDIAN)  # 5,760만원/12 ÷ 1인 중위 = 187%
    assert _result(f, "가구 소득(중위소득 %)") == NOT_MET


def test_parents_household_policy_is_unknown():
    p = _policy(income_basis="가구", condition_confidence="high", household_median_pct_max=100,
                income_text="원가구 기준중위소득 100% 이하")
    assert _result(judge(p, _customer(), MEDIAN), "가구 소득(중위소득 %)") == UNKNOWN


def test_freelancer_income_is_approximate_and_borderline_unknown():
    lee = _customer(customer_type="사업·프리랜서", employment_status="프리랜서",
                    income={("사업", "사업소득금액"): {"amount_won": 52_000_000, "tax_year": 2025, "status": "기재"}})
    p = _policy(income_basis="본인", condition_confidence="high", annual_income_max_won=50_000_000)
    cond = next(x for x in judge(p, lee, MEDIAN)["conditions"] if x["condition"] == "본인 연소득")
    assert cond["approximate"] and cond["result"] == UNKNOWN  # 기준선 ±10% 안


def test_freelancer_never_uses_salary_metric():
    lee = _customer(customer_type="사업·프리랜서", employment_status="프리랜서",
                    income={("사업", "사업소득금액"): {"amount_won": 217_000_000, "tax_year": 2025, "status": "기재"}})
    p = _policy(income_basis="본인", condition_confidence="high", annual_income_max_won=50_000_000)
    cond = next(x for x in judge(p, lee, MEDIAN)["conditions"] if x["condition"] == "본인 연소득")
    assert cond["result"] == NOT_MET and "사업소득금액" in cond["basis"]


def test_job_code_mismatch():
    f = judge(_policy(job_cd="0013003"), _customer(), MEDIAN)  # 미취업자 대상
    assert _result(f, "취업 상태") == NOT_MET


def test_unknown_income_text_needs_check():
    f = judge(_policy(income_basis="미상", condition_confidence="none", income_text="유형별 상이"), _customer(), MEDIAN)
    assert f["verdict"] == "확인 필요"


def test_name_based_groups():
    newlywed = _policy(policy_name="신혼부부 전세자금 이자지원")
    assert judge(newlywed, _customer(goals="비상금"), MEDIAN)["verdict"] == "불충족"
    assert judge(newlywed, _customer(), MEDIAN)["verdict"] == "확인 필요"  # 결혼 목표가 있으면 남긴다
    assert judge(_policy(policy_name="신생아 특례 대출"), _customer(), MEDIAN)["verdict"] == "불충족"
    assert judge(_policy(policy_name="대학생 학자금 지원"), _customer(), MEDIAN)["verdict"] == "확인 필요"


def test_women_only_policy():
    f = judge(_policy(special_cd="0014002"), _customer(), MEDIAN)
    assert _result(f, "특화 대상") == NOT_MET


def test_homeless_requirement():
    f = judge(_policy(search_text="무주택 청년 월세 지원"), _customer(home_owner=True), MEDIAN)
    assert _result(f, "무주택") == NOT_MET
