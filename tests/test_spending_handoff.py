from customer.spending_handoff import build, estimate_effect


def _finding(name, btype, verdict="입력상 충족", unknown=None, **benefit):
    b = {"type": btype, "summary": f"[{btype}]", "monthly_won": None, "duration_months": None, "total_won": None,
         "range_won": None, "loan_limit_won": None, "interest_support_pct": None, "discount_pct": None,
         "discount_won": None, "match_ratio": None, "own_saving_monthly_won": None}
    b.update(benefit)
    conds = [{"condition": "연령", "result": "충족", "note": "", "policy_value": ""}]
    if unknown:
        conds.append({"condition": unknown, "result": "미확인", "note": "확인", "policy_value": ""})
    return {"policy_id": name, "policy_name": name, "verdict": verdict, "benefit": b, "conditions": conds,
            "apply": {"apply_end_date": None, "recruit_status": "always", "url": "u"}}


CASHFLOW = [
    {"section": "저축", "item": "적금", "monthly_won": 700_000, "purpose": None, "necessity": None, "detail": None},
    {"section": "고정지출", "item": "월 임대료", "monthly_won": 650_000, "purpose": "생활", "necessity": "필수", "detail": None},
    {"section": "고정지출", "item": "교통비", "monthly_won": 100_000, "purpose": "생활", "necessity": None,
     "detail": "대중교통 8 + 택시 2(만원)"},
    {"section": "변동지출", "item": "자기개발비", "monthly_won": 100_000, "purpose": "생활", "necessity": None, "detail": None},
    {"section": "변동지출", "item": "문화생활비", "monthly_won": 0, "purpose": "생활", "necessity": None, "detail": None},
]


def _result(findings, other=()):
    return {"case_id": "t", "profile_version": "v1", "generated_at": "x", "findings": list(findings),
            "other_findings": list(other), "questions": [], "search_scope": {"policy_db_as_of": "20261104"}}


def _line(out, item):
    return next(l for l in out["expense_lines"] if l["item"] == item)


def test_transit_refund_uses_public_transit_part_only():
    out = build(_result([], [_finding("K-패스", "할인·환급", discount_pct=30)]), CASHFLOW, [])
    line = _line(out, "교통비")
    assert line["scenario_monthly_saving_won"] == 24_000  # 대중교통 8만원 × 30%
    assert out["totals"]["scenario_monthly_saving_won"] == 24_000


def test_needs_check_goes_to_potential_only():
    out = build(_result([_finding("청년 월세 지원", "월세·주거비", verdict="확인 필요", unknown="소득",
                                  monthly_won=200_000, duration_months=12)]), CASHFLOW, [])
    line = _line(out, "월 임대료")
    assert line["scenario_monthly_saving_won"] == 0
    assert line["potential_monthly_saving_won"] == 200_000


def test_same_line_policies_are_not_summed():
    out = build(_result([_finding("A 월세 지원", "월세·주거비", monthly_won=200_000),
                         _finding("B 월세 지원", "월세·주거비", monthly_won=100_000)]), CASHFLOW, [])
    assert _line(out, "월 임대료")["scenario_monthly_saving_won"] == 200_000


def test_partial_category_rate_is_a_range():
    eff = estimate_effect({"discount_pct": 50}, {"item": "자기개발비", "monthly_won": 100_000,
                                                "category": "자기개발비", "detail": None})
    assert eff["monthly_saving_won"] == [0, 50_000]


def test_one_off_is_not_monthly_saving():
    out = build(_result([_finding("평생교육이용권", "바우처·현물", total_won=350_000)]), CASHFLOW, [])
    line = _line(out, "자기개발비")
    assert line["scenario_monthly_saving_won"] == 0
    assert line["policy_offsets"][0]["effect"]["one_off_won"] == [350_000, 350_000]


def test_zero_spend_lines_get_no_offsets_and_targeted_groups_are_listed():
    out = build(_result([_finding("경기 컬처패스", "할인·환급", total_won=60_000),
                         _finding("예술인 창작 지원", "현금지원", verdict="확인 필요", unknown="대상 집단(정책명)",
                                  total_won=9_000_000)]), CASHFLOW, [])
    assert _line(out, "문화생활비")["policy_offsets"] == []
    assert out["excluded"]["targeted_group_unverified"] == ["예술인 창작 지원"]


def test_savings_and_debt_are_split_out():
    out = build(_result([_finding("청년내일저축계좌", "매칭저축", own_saving_monthly_won=100_000, match_ratio=3),
                         _finding("청년 전월세 대출보증", "대출", loan_limit_won=200_000_000)]), CASHFLOW, [])
    assert out["savings_programs"][0]["own_saving_monthly_won"] == 100_000
    assert out["excluded"]["debt_instruments"] == 1


def test_training_programs_are_not_spending_offsets():
    out = build(_result([], [_finding("국토교통 과학기술 신진연구자 지원사업", "현금지원")]), CASHFLOW, [])
    assert _line(out, "교통비")["policy_offsets"] == []
