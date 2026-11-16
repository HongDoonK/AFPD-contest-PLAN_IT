"""planning.reflect 시험. 김도윤 실제 입력(파일)과 합성 입력을 쓴다. DB·웹은 쓰지 않는다."""
import copy
import json
from pathlib import Path

import pytest

from customer.profile import load_case
from planning.allocation import allocate
from planning.reflect import build, main, reflect
from planning.spending_cut import spending_cut

ROOT = Path(__file__).resolve().parents[1]
STEP2_PATH = ROOT / "planning" / "cases" / "kim_doyun_step2.json"
CASE_PATH = ROOT / "customer" / "cases" / "kim_doyun.json"
POLICY_PATH = ROOT / "output" / "policy_kim_doyun_v2.json"

needs_kim = pytest.mark.skipif(not POLICY_PATH.exists(), reason="output/policy_kim_doyun_v2.json 없음")


@pytest.fixture(scope="module")
def kim():
    step2 = json.loads(STEP2_PATH.read_text(encoding="utf-8"))
    profile = load_case(CASE_PATH)
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8")) if POLICY_PATH.exists() else None
    return step2, profile, policy


@pytest.fixture(scope="module")
def kim_cut(kim):
    step2, profile, policy = kim
    if policy is None:
        pytest.skip("정책 결과 파일 없음")
    return spending_cut(profile, policy, copy.deepcopy(step2))


def kim_result(kim, scenario):
    step2, profile, policy = kim
    return build(copy.deepcopy(step2), profile, policy, scenario)


# ── 구조·불변 ─────────────────────────────────────────────────
@needs_kim
@pytest.mark.parametrize("scenario", ["certain", "potential"])
def test_top_keys_order_and_values_preserved(kim, scenario):
    step2 = kim[0]
    out = kim_result(kim, scenario)
    top = [k for k in step2 if k != "_meta"]
    assert list(out) == top + ["_allocation", "_meta"]
    assert all(out[k] == step2[k] for k in top if k != "deltaM")
    meta_keys = list(step2["_meta"])
    assert list(out["_meta"]) == meta_keys + ["policy_reflection"]
    for k in meta_keys:
        if k != "deltaM_detail":
            assert out["_meta"][k] == step2["_meta"][k]


@needs_kim
def test_inputs_not_mutated(kim, kim_cut):
    step2, profile, _ = kim
    s, c = copy.deepcopy(step2), copy.deepcopy(kim_cut)
    alloc = allocate(copy.deepcopy(step2), profile)
    a = copy.deepcopy(alloc)
    out = reflect(s, c, alloc, "potential")
    assert s == step2 and c == kim_cut and alloc == a
    out["_meta"]["deltaM_detail"]["messages"].append("x")
    assert step2["_meta"]["deltaM_detail"]["messages"] == []


# ── 김도윤 반영값 ─────────────────────────────────────────────
@needs_kim
@pytest.mark.parametrize("scenario,dm,a,fixed,var", [("certain", 1812.0, 151.0, 140.4, 136.6),
                                                     ("potential", 1860.0, 155.0, 140.4, 132.6)])
def test_kim_reflected_values(kim, scenario, dm, a, fixed, var):
    out = kim_result(kim, scenario)
    d = out["_meta"]["deltaM_detail"]
    assert out["deltaM"] == dm
    assert (d["a_monthly"], d["fixed_after_monthly"], d["variable_after_monthly"]) == (a, fixed, var)
    assert (d["income_monthly"], d["cut_monthly"], d["status"]) == (428.0, 17.1, "OK")
    assert round(d["income_monthly"] - d["fixed_after_monthly"] - d["variable_after_monthly"], 2) == d["a_monthly"]
    assert d["messages"] == []
    r = out["_meta"]["policy_reflection"]
    assert r["before"] == {"deltaM": 1783.2, "a_monthly": 148.6, "fixed_after_monthly": 142.8,
                           "variable_after_monthly": 136.6}
    assert r["change"]["deltaM"] == round(dm - 1783.2, 2)
    assert r["scenario"] == scenario and r["module"] == "reflect"


@needs_kim
def test_kim_applied_policies(kim):
    cert = kim_result(kim, "certain")["_meta"]["policy_reflection"]
    pot = kim_result(kim, "potential")["_meta"]["policy_reflection"]
    assert [(p["policy_name"], p["monthly_saving"]) for p in cert["applied_policies"]] == [("K-패스(K패스)", 2.4)]
    assert cert["applied_policies"][0]["verdict"] == "입력상 충족" and cert["applied_policies"][0]["policy_id"]
    assert len(pot["applied_policies"]) == 2
    assert round(sum(p["monthly_saving"] for p in pot["applied_policies"]), 2) == 6.4
    assert pot["other_scenario"] == {"scenario": "certain", "deltaM": 1812.0, "a_monthly": 151.0,
                                     "shortfall_monthly": 314.77}


@needs_kim
@pytest.mark.parametrize("scenario,shortfall,surplus", [("certain", 314.77, 151.0), ("potential", 310.77, 155.0)])
def test_allocation_uses_reflected_deltaM(kim, scenario, shortfall, surplus):
    al = kim_result(kim, scenario)["_allocation"]
    assert al["feasibility"]["shortfall_monthly"] == shortfall and al["feasibility"]["monthly_surplus"] == surplus
    assert al["feasibility_before"]["shortfall_monthly"] == 317.17
    assert al["distributable"] == 4058.0 and al["m_avail"] == 4700.0
    assert set(al["goals"][0]) == {"goal", "name", "need", "existing", "gap", "years", "ratio", "ratio_by_need",
                                   "amount", "coverage", "monthly_needed", "active"}


# ── 오류 ──────────────────────────────────────────────────────
@needs_kim
def test_double_reflection_rejected(kim, kim_cut):
    out = kim_result(kim, "certain")
    with pytest.raises(ValueError, match="이중 반영"):
        reflect(out, kim_cut, allocate(copy.deepcopy(kim[0]), kim[1]))


@needs_kim
def test_bad_scenario_rejected(kim, kim_cut):
    with pytest.raises(ValueError, match="scenario"):
        reflect(kim[0], kim_cut, {}, "maybe")
    with pytest.raises(ValueError, match="scenario"):
        build(kim[0], kim[1], kim[2], "both")


@needs_kim
def test_cut_without_step2_link_rejected(kim):
    step2, profile, policy = kim
    with pytest.raises(ValueError, match="step2_link"):
        reflect(step2, spending_cut(profile, policy), {})


# ── 지원 기간 12개월 미만(합성) ────────────────────────────────
def _finding(name, **benefit):
    b = {"type": "할인·환급", "summary": "요약", "monthly_won": None, "duration_months": None, "total_won": None,
         "range_won": None, "discount_pct": None, "discount_won": None}
    b.update(benefit)
    return {"policy_id": f"id-{name}", "policy_name": name, "verdict": "입력상 충족", "conditions": [], "benefit": b,
            "apply": {"apply_end_date": None, "recruit_status": "always", "url": "https://example.test"}}


SYN_PROFILE = {
    "case_id": "t", "profile_version": "v1", "as_of_date": "2026-11-12", "goals": [], "liabilities": [],
    "facts": {k: {"value": v} for k, v in {"birth_date": "1992-04-18", "sex": "남", "region_sido": "경기",
                                           "marital_status": "미혼", "home_owner": False}.items()},
    "cashflow_items": [
        {"section": "변동지출", "item": "생활비", "monthly_won": 800_000, "purpose": "생활", "necessity": None, "detail": None},
        {"section": "고정지출", "item": "통신비", "monthly_won": 50_000, "purpose": "생활", "necessity": None, "detail": None},
        {"section": "보험", "item": "보장성보험", "monthly_won": 200_000, "purpose": "생활", "necessity": None, "detail": None},
    ],
}
# income 400 − fixed 25 − variable 80 = a 295, deltaM = 295 × 12
SYN_STEP2 = {"p": 0.0, "q": 0.0, "r": 0.0, "s": 0.0, "M": 3000.0, "deltaM": 3540.0,
             "_meta": {"deltaM_detail": {"a_monthly": 295.0, "income_monthly": 400.0, "fixed_after_monthly": 25.0,
                                         "variable_after_monthly": 80.0, "cut_monthly": 0.0, "status": "OK",
                                         "messages": ["원본 메시지"]},
                       "asset_detail": {"M_items": ["현금 3000"]}}}


def _syn_policy(months):
    return {"case_id": "t", "profile_version": "v1", "generated_at": "x", "search_scope": {"policy_db_as_of": "x"},
            "findings": [_finding("통신 요금 할인", monthly_won=10_000, duration_months=months)],
            "other_findings": [], "situational_findings": [], "questions": []}


def test_short_duration_adds_message():
    out = build(copy.deepcopy(SYN_STEP2), SYN_PROFILE, _syn_policy(6))
    d = out["_meta"]["deltaM_detail"]
    assert out["deltaM"] == 3546.0 and d["a_monthly"] == 295.5 and d["fixed_after_monthly"] == 24.0
    assert d["messages"][0] == "원본 메시지" and len(d["messages"]) == 2 and "12개월 미만" in d["messages"][1]
    assert SYN_STEP2["_meta"]["deltaM_detail"]["messages"] == ["원본 메시지"]
    assert out["_meta"]["policy_reflection"]["applied_policies"][0]["duration_months"] == 6


def test_full_year_policy_keeps_identity():
    d = build(copy.deepcopy(SYN_STEP2), SYN_PROFILE, _syn_policy(None))["_meta"]["deltaM_detail"]
    assert d["a_monthly"] == 296.0 and d["messages"] == ["원본 메시지"]
    assert d["policy_cut_monthly"] == {"variable": 0.0, "fixed": 1.0}


# ── CLI ───────────────────────────────────────────────────────
def _argv(out, scenario="both", *extra):
    return ["--step2", str(STEP2_PATH), "--case", str(CASE_PATH), "--policy", str(POLICY_PATH),
            "--scenario", scenario, "--out", str(out), *extra]


@needs_kim
def test_cli_both_writes_two_files_and_refuses_overwrite(tmp_path):
    pattern = tmp_path / "r_{scenario}.json"
    main(_argv(pattern))
    files = {s: tmp_path / f"r_{s}.json" for s in ("certain", "potential")}
    assert all(f.exists() for f in files.values())
    assert json.loads(files["potential"].read_text(encoding="utf-8"))["deltaM"] == 1860.0
    with pytest.raises(SystemExit):
        main(_argv(pattern))
    main(_argv(pattern, "both", "--force"))


@needs_kim
def test_cli_both_requires_placeholder(tmp_path):
    with pytest.raises(SystemExit):
        main(_argv(tmp_path / "r.json"))
    assert not (tmp_path / "r.json").exists()


@needs_kim
def test_cli_passes_allocate_options(tmp_path):
    out = tmp_path / "c.json"
    main(_argv(out, "certain", "--retire-age", "60"))
    r = json.loads(out.read_text(encoding="utf-8"))
    assert r["_meta"]["policy_reflection"]["sources"]["allocate_options"] == {"retire_age": 60.0}
    assert any("인자" in w for w in r["_allocation"]["warnings"])
