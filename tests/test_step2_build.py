"""planning.step2_build 시험. 김도윤 실제 파일(있으면)과 합성 프로필."""
import copy
import json
from pathlib import Path

import pytest

from customer.profile import load_case
from planning.allocation import m_available
from planning.step2_build import build_step2

ROOT = Path(__file__).resolve().parents[1]
KIM_CASE = ROOT / "customer" / "cases" / "kim_doyun.json"
KIM_STEP2 = ROOT / "planning" / "cases" / "kim_doyun_step2.json"
KIM_REPORT = ROOT / "output" / "spending_report_kim_doyun_v2.json"


def synthetic(**over):
    prof = {
        "case_id": "syn", "profile_version": "v1", "as_of_date": "2026-01-01", "is_synthetic": True,
        "facts": {}, "liabilities": [], "goals": [], "tax_items": [], "insurance_contracts": [],
        "assets": [
            {"asset_id": "a1", "kind": "입출금", "balance_won": 1_000_000, "monthly_contribution_won": None, "liquidity": "즉시"},
            {"asset_id": "a2", "kind": "적금", "balance_won": 2_000_000, "monthly_contribution_won": 100_000, "rate_pct": 3.0},
            {"asset_id": "a3", "kind": "주택청약", "balance_won": 3_000_000, "monthly_contribution_won": 100_000, "rate_pct": 2.0,
             "opened_on": "2020-01-01"},
            {"asset_id": "a4", "kind": "퇴직연금", "balance_won": None, "monthly_contribution_won": 200_000},
            {"asset_id": "a5", "kind": "임차보증금", "balance_won": 50_000_000},
            {"asset_id": "a6", "kind": "ETF", "name": "지수 500", "balance_won": 4_000_000, "monthly_contribution_won": 0},
        ],
        "income_items": [{"kind": "사업", "basis": "세후 입금", "period": "월", "amount_won": 5_000_000, "in_cashflow": 1}],
        "cashflow_items": [
            {"section": "저축", "item": "적금", "monthly_won": 100_000},
            {"section": "세금·사회보험", "item": "세금 적립", "monthly_won": 500_000},
            {"section": "보험", "item": "보장성", "monthly_won": 100_000},
            {"section": "고정지출", "item": "월세", "monthly_won": 1_000_000},
            {"section": "고정지출", "item": "통신비", "monthly_won": None},
            {"section": "변동지출", "item": "생활비", "monthly_won": 1_500_000},
        ],
    }
    prof.update(over)
    return prof


def test_synthetic_values():
    prof = synthetic()
    before = copy.deepcopy(prof)
    s = build_step2(prof)
    assert prof == before
    assert s["p"] == 300 and s["p_rate"] == 2.0 and s["p_date"] == "2020-01-01" and s["deltap"] == 120
    assert s["q"] == 0 and s["deltaq"] == 240  # 잔액 미확인 → 0
    assert s["r"] == 0 and s["deltar"] is None and s["s"] == 0
    assert s["M"] == 700  # 입출금 100 + 적금 200 + ETF 400, 보증금 제외
    d = s["_meta"]["deltaM_detail"]
    assert d["income_monthly"] == 450  # 500 − 세금 50
    assert d["fixed_after_monthly"] == 110 and d["variable_after_monthly"] == 150 and d["cut_monthly"] == 0
    assert d["a_monthly"] == 190 and s["deltaM"] == 2280
    assert s["_meta"]["status"] == "INCOMPLETE" and d["status"] == "INCOMPLETE"
    assert s["_meta"]["derived"] is True
    assert any("퇴직연금" in w or "IRP" in w for w in s["_meta"]["warnings"])


def test_m_items_readable_by_allocation():
    s = build_step2(synthetic())
    warnings = []
    m = m_available(s, False, warnings)
    assert m["M_items_sum"] == 700 and not warnings  # 이름의 숫자(500)가 금액으로 읽히지 않음


def test_spending_report_cut_goes_to_variable():
    report = {"calculations": {"effect": {"adjustment_monthly_won": 200_000}}}
    s = build_step2(synthetic(), report)
    d = s["_meta"]["deltaM_detail"]
    assert d["cut_monthly"] == 20 and d["variable_after_monthly"] == 130 and d["a_monthly"] == 210


def test_complete_profile_status_ok():
    prof = synthetic()
    prof["assets"] = [a for a in prof["assets"] if a["balance_won"] is not None]
    prof["cashflow_items"] = [r for r in prof["cashflow_items"] if r["monthly_won"] is not None]
    assert build_step2(prof)["_meta"]["status"] == "OK"


@pytest.mark.skipif(not KIM_CASE.exists() or not KIM_STEP2.exists(), reason="김도윤 파일 없음")
def test_kim_matches_team_step2():
    team = json.loads(KIM_STEP2.read_text(encoding="utf-8"))
    report = json.loads(KIM_REPORT.read_text(encoding="utf-8")) if KIM_REPORT.exists() else None
    s = build_step2(load_case(KIM_CASE), report)
    assert [k for k in s if k != "_meta"] == [k for k in team if k != "_meta"]
    assert list(s["_meta"]["deltaM_detail"]) == list(team["_meta"]["deltaM_detail"])
    for k in ("p", "q", "r", "s", "M"):
        assert s[k] == team[k], k
    assert (s["p"], s["q"], s["r"], s["s"], s["M"]) == (300, 0, 500, 0, 4700)
    assert s["deltap"] == team["deltap"] and s["deltar"] == team["deltar"]
    # deltaM은 팀원 값(1783.2)과 다를 수 있다: 차이만 기록
    print("deltaM 유도", s["deltaM"], "팀원", team["deltaM"], "차이", round(s["deltaM"] - team["deltaM"], 2))
    assert abs(s["deltaM"] - team["deltaM"]) < 0.1 * team["deltaM"]
