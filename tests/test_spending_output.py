import json
from pathlib import Path

import pytest
from conftest import offset

from spending_report.llm import NoLLM
from spending_report.render import change, manwon, to_html, ymd
from spending_report.report import build_report

KPASS = ("고정지출", "교통비", [offset("K-패스(K패스)", 24_000)])
LEE_FF = Path(__file__).resolve().parents[1] / "가상고객데이터" / "가상 고객 설정2.docx"


def _report(inp, root=None):
    kwargs = {"root": root} if root else {}
    return build_report(inp, NoLLM(), log_file="ai.json", **kwargs)[0]


def test_result_follows_module_contract(make_input):
    report = _report(make_input("kim_doyun", [KPASS]))
    for key in ("module", "profile_version", "status", "findings", "calculations", "evidence", "missing_fields", "questions",
                "recommendations", "narrative", "review", "ai_usage", "limitations"):
        assert key in report
    assert report["module"] == "spending"
    assert report["status"] == "partial"
    assert report["review"]["state"] == "담당자 검토 전"
    assert report["ai_usage"] == {"provider": "none", "model": None, "sentences_llm": 0,
                                  "sentences_template": 1 + len(report["narrative"]["coaching"]), "log_file": "ai.json"}
    assert "employer_size" in report["missing_fields"]
    assert "정책 질문" in report["questions"]


def test_output_is_reproducible(make_input):
    inp = make_input("kim_doyun", [KPASS])
    first, second = _report(inp), _report(inp)
    first.pop("generated_at"), second.pop("generated_at")
    assert first == second


def test_unreadable_document_is_marked_not_fatal(make_input, tmp_path):
    report = _report(make_input("kim_doyun"), root=tmp_path)
    assert {d["status"] for d in report["evidence"]["documents"].values()} == {"미검증"}
    assert report["status"] == "partial"


@pytest.mark.skipif(not LEE_FF.exists(), reason="원본 문서 없음")
def test_document_mismatch_needs_review(make_input):
    def edit(profile):
        next(r for r in profile["cashflow_items"] if r["item"] == "교통비")["monthly_won"] = 70_000
    report = _report(make_input("lee_seojun", mutate=edit))
    assert report["status"] == "needs_review"
    assert report["evidence"]["documents"]["fact_finding"]["status"] == "불일치"
    assert any("원본 문서" in q for q in report["questions"])


def test_one_off_policies_are_listed_but_not_in_the_table(make_input):
    one_off = [{"policy_id": "p", "policy_name": "면접수당", "certainty": "시나리오 사용 가능", "open_conditions": [],
                "effect": {"kind": "일시금", "one_off_won": [150_000, 150_000], "basis": "b", "assumptions": []},
                "action": {"deadline": "20261231", "url": "www.example.test"}}]
    report = _report(make_input("kim_doyun", one_off=one_off))
    benefits = report["findings"]["policy_benefits"]
    assert benefits["other_scenario"][0]["effect"] == "일시금 15만원"
    assert report["calculations"]["effect"]["policy_monthly_won"] == 0
    assert "2026-12-31" in to_html(report) and "https://www.example.test" in to_html(report)


def test_move_in_housing_is_left_out_of_the_spending_report(make_input):
    lines = [("고정지출", "월 임대료", [offset("청년하우스 입주", kind="주거이전", certainty="확인 필요")])]
    benefits = _report(make_input("kim_doyun", lines))["findings"]["policy_benefits"]
    assert benefits["needs_check"] == [] and benefits["excluded_counts"]["housing_move"] == 1


def test_tax_reference_never_claims_tax_change(make_input):
    tax = _report(make_input("kim_doyun"))["findings"]["income_tax_reference"]
    assert tax["after_effect"] == "미계산"


@pytest.mark.parametrize("case", ["kim_doyun", "lee_seojun"])
def test_html_shows_every_cashflow_row(make_input, case):
    report = _report(make_input(case, [KPASS] if case == "kim_doyun" else []))
    html = to_html(report)
    for block in report["calculations"]["cashflow"]["blocks"]:
        for row in block["rows"]:
            assert row["label"] in html
    for phrase in ("소비습관 개선 보고서", "적용 전", "적용 후", "담당자 검토 전", "시뮬레이션", "가상 고객"):
        assert phrase in html


def test_html_escapes_outside_text(make_input):
    html = to_html(_report(make_input("kim_doyun", [("고정지출", "교통비", [offset("<script>alert(1)</script>", 24_000)])])))
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_number_formats():
    assert manwon(18_333_300) == "1,833.33"
    assert manwon(5_051_750) == "505.175"
    assert manwon(0) == "0"
    assert manwon(None) == "미확인"
    assert change(-34_000) == "−3.4" and change(176_000) == "+17.6" and change(0) == ""
    assert ymd("20261231") == "2026-12-31" and ymd(None) == ""
