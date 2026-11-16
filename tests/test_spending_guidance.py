from pathlib import Path

import pytest
from conftest import offset

from spending_report.engine import analyze_rows, calculate
from spending_report.guidance import action_plan, add_months, data_requests, deduction_flags
from spending_report.llm import NoLLM
from spending_report.narrative import won_text
from spending_report.render import to_html
from spending_report.report import build_report

KPASS = ("고정지출", "교통비", [offset("K-패스(K패스)", 24_000)])
BASE = Path(__file__).resolve().parents[1] / "가상고객데이터" / "빈칸형_가상고객_4명"


def _run(inp):
    analysis = analyze_rows(inp)
    return analysis, calculate(inp, analysis)


def test_diagnosis_matches_hand_calculation(make_input):
    _, calc = _run(make_input("kim_doyun", [KPASS]))
    dx = calc["diagnosis"]
    assert dx["consumption_won"] == 2_740_000
    groups = {g["necessity"]: (g["before_won"], g["after_won"]) for g in dx["by_necessity"]}
    assert groups == {"필수": (1_535_000, 1_511_000), "일부조정": (305_000, 233_000),
                      "선택": (500_000, 420_000), "미확인": (400_000, 400_000)}
    assert dx["flexible_share_pct"] == 29.4
    assert [t["label"] for t in dx["top_flexible"][:2]] in (["쇼핑", "배달·외식"], ["쇼핑", "문화생활비"])
    assert sum(g["change_won"] for g in dx["by_necessity"]) == -calc["effect"]["monthly_freed_won"]


def test_recommendations_are_sorted_with_tradeoffs(make_input):
    analysis, _ = _run(make_input("kim_doyun", [KPASS]))
    changes = [r["amount"]["monthly_change_won"] for r in analysis.recommendations]
    assert changes == sorted(changes)
    taxi = next(r for r in analysis.recommendations if r["target"] == "택시")
    assert taxi["tradeoff"] and taxi["when"] == "이번 달부터"


def test_action_plan_has_schedule_and_targets(make_input):
    inp = make_input("kim_doyun", [KPASS])
    analysis, calc = _run(inp)
    plan = action_plan(inp, calc, analysis, {"savings_programs": []}, [])
    assert plan["first_check"] == "2026-12-02" and plan["review"] == "2027-02-02"
    delivery = next(i for i in plan["items"] if i["target"] == "배달·외식")
    assert delivery["check"] == "월 15만원 이하" and delivery["owner"] == "고객"
    assert plan["items"][-1]["owner"] == "담당자"
    assert plan["metrics"][0] == {"name": "절감 재원 별도 이체액", "target_won": 176_000}
    assert any("2027-04-18" in t for t in plan["review_triggers"])


def test_add_months():
    assert add_months("2026-11-30", 3) == "2027-02-28"
    assert add_months("2026-12-02", 1) == "2027-01-02"


def test_complete_customer_needs_no_data_requests(make_input):
    inp = make_input("kim_doyun", [KPASS])
    analysis, calc = _run(inp)
    assert data_requests(inp, calc, analysis) == []


def test_deduction_flags_compare_ff_with_receipt(make_input):
    inp = make_input("kim_doyun")
    fields = {"주택청약공제": {"value": 0}, "연금저축공제": {"value": 144_000}, "퇴직연금공제": {"value": 0}, "월세공제": {"value": 0},
              "차감징수_소득세": {"value": -334_020}, "차감징수_지방소득세": {"value": -33_410}}
    flags = deduction_flags(inp, ("근로", fields))
    assert [f["item"] for f in flags] == ["주택청약 소득공제", "월세액 세액공제", "연말정산 환급"]
    assert "367,430원" in flags[2]["finding"]
    assert deduction_flags(inp, ("사업", {})) == [] and deduction_flags(inp, None) == []


@pytest.mark.parametrize("won,text", [(216_600, "21.66만원"), (11_185_500, "1,118.55만원"), (18_600, "18,600원"), (24_000, "2.4만원"), (367_430, "367,430원")])
def test_money_text_uses_manwon(won, text):
    assert won_text(won) == text


def test_complete_report_has_new_sections(make_input):
    report, _ = build_report(make_input("kim_doyun", [KPASS]), NoLLM())
    html = to_html(report)
    for title in ("현재 소비 진단", "소비습관 개선안", "실행과 점검 계획", "확인이 필요한 사항", "부록", "고려할 점"):
        assert title in html
    assert report["calculations"]["diagnosis"]["available"] is True
    assert report["data_requests"] == []


@pytest.mark.skipif(not BASE.exists(), reason="빈칸형 문서 없음")
def test_blank_customer_gets_prioritized_requests(tmp_path):
    import json
    from conftest import handoff_for

    from customer.from_documents import build_case
    from customer.profile import connect, load_case, store
    from spending_report.inputs import load_input

    root = Path(__file__).resolve().parents[1]
    case = build_case("t_minji", BASE / "01_김민지", root)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    profile = load_case(path)
    conn = connect(tmp_path / "c.db")
    store(conn, profile)
    lines = [("고정지출", "교통비", [offset("K-패스(K패스)", kind="지출미확인")])]
    conn.execute("INSERT INTO module_results VALUES (?,?,?,?,?,?)",
                 ("t_minji", "v1", "policy_to_spending", "completed", json.dumps(handoff_for("t_minji", "v1", lines), ensure_ascii=False), "t"))
    conn.commit()
    report, _ = build_report(load_input(conn, "t_minji", "v1"), NoLLM(), root=root)
    items = [q["item"] for q in report["data_requests"]]
    assert items[0] == "최근 3개월 급여 명세" and items[1] == "카드 명세서 최근 3개월"
    assert "교통비 월 금액" in items and "대출 상환표(원금, 이자, 금리)" in items
    transit = next(q for q in report["data_requests"] if q["item"] == "교통비 월 금액")
    assert "K-패스" in transit["unlocks"]
    assert report["calculations"]["diagnosis"] == {"available": False}
    assert not any(q.startswith("금액이 비어") for q in report["questions_other"])
    assert any("주택청약" in f["item"] for f in report["findings"]["deduction_flags"])
    html = to_html(report)
    assert "받을 자료" in html and "계산 불가" not in html.split("현금흐름표 적용 전")[1].split("실행과 점검")[0]
