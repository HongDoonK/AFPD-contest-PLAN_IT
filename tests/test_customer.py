import copy
from pathlib import Path

import pytest

from customer.profile import age_on, connect, load_case, policy_inputs, store, validate

CASES = Path(__file__).resolve().parents[1] / "customer" / "cases"
MEDIAN = {1: 2_564_238}


@pytest.fixture(params=["kim_doyun", "lee_seojun"])
def profile(request):
    return load_case(CASES / f"{request.param}.json")


def test_cases_pass_validation(profile):
    assert [i for i in validate(profile) if i["level"] == "error"] == []


def test_sns_revenue_added_twice_is_caught():
    p = load_case(CASES / "lee_seojun.json")
    sns = next(r for r in p["income_items"] if r["payer"] == "SNS")
    p["income_items"].append(copy.deepcopy(sns))
    assert any(i["check"] == "revenue_breakdown" for i in validate(p))


def test_cashflow_mismatch_is_caught():
    p = load_case(CASES / "kim_doyun.json")
    p["cashflow_items"][0]["monthly_won"] += 10_000
    assert any(i["check"] == "cashflow_balance" and i["level"] == "error" for i in validate(p))


def test_tax_reserve_counted_once_in_assets():
    p = load_case(CASES / "lee_seojun.json")
    p["assets"].append({**p["assets"][-2], "asset_id": "dup"})  # 세금준비금을 한 번 더 넣으면
    assert any(i["check"] == "asset_total" for i in validate(p))


def test_unknown_value_is_not_zero(profile):
    f = profile["facts"]["military_service"]
    assert f["value"] is None and f["status"] == "미확인"


def test_age_is_full_years():
    assert age_on("1992-04-18", "2026-11-02") == 34
    assert age_on("1992-04-18", "2027-04-17") == 34
    assert age_on("1992-04-18", "2027-04-18") == 35


def test_policy_inputs_keep_income_metrics_apart():
    kim = policy_inputs(load_case(CASES / "kim_doyun.json"), median_monthly=MEDIAN)
    assert kim["customer_type"] == "근로"
    assert kim["income_metrics"]["총급여"]["annual_won"] == 57_600_000
    assert kim["income_metrics"]["총급여"]["median_pct_of_household"] == 187.2
    assert "사업소득금액" not in kim["income_metrics"]

    lee = policy_inputs(load_case(CASES / "lee_seojun.json"), median_monthly=MEDIAN)
    assert lee["customer_type"] == "사업·프리랜서"
    assert "총급여" not in lee["income_metrics"]  # 매출을 총급여로 바꿔 넣지 않는다
    assert "employer_size" not in lee["missing_fields"]
    assert any("종합소득금액" in q for q in lee["questions"])


def test_store_keeps_other_versions(tmp_path):
    conn = connect(tmp_path / "c.db")
    p = load_case(CASES / "kim_doyun.json")
    p["profile_version"] = "v1"
    store(conn, p)
    v2 = copy.deepcopy(p)
    v2["profile_version"] = "v2"
    v2["facts"]["military_service"] = {"value": "육군 18개월", "unit": None, "status": "기재",
                                       "source_ref": "추가 질문 답변", "assumptions": None}
    store(conn, v2)
    store(conn, v2)  # 같은 버전을 다시 넣어도 중복되지 않는다
    rows = conn.execute("SELECT profile_version, COUNT(*) FROM assets GROUP BY 1 ORDER BY 1").fetchall()
    assert rows == [("v1", 8), ("v2", 8)]
    assert conn.execute("SELECT value_text FROM profile_facts WHERE profile_version='v2' "
                        "AND field='military_service'").fetchone() == ("육군 18개월",)
