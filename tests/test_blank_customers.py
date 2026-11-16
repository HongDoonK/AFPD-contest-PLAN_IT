"""고객이 직접 적어 빈칸과 어림값이 많은 문서(빈칸형 4명)와 HWP 입력."""
import json
from pathlib import Path

import pytest
from conftest import handoff_for

from customer.from_documents import build_case, resolve_region
from customer.profile import connect, load_case, store, validate
from customer.spending_handoff import build as build_handoff, estimate_effect
from spending_report import intake
from spending_report.engine import analyze_rows, calculate
from spending_report.hwp import HwpError, hwp_tables
from spending_report.inputs import load_input
from spending_report.llm import NoLLM
from spending_report.render import cell_text, to_html
from spending_report.report import build_report

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "가상고객데이터" / "빈칸형_가상고객_4명"
KIM_HWP = ROOT / "가상고객데이터" / "01_FACT_FINDING_김도윤_가상학습용.hwp"
need_blank = pytest.mark.skipif(not BASE.exists(), reason="빈칸형 문서 없음")
FOLDERS = {"minji": "01_김민지", "junho": "02_박준호", "seoyeon": "03_이서연", "hyunwoo": "04_김현우"}


@pytest.mark.parametrize("text,won,low,high,status", [
    ("", None, None, None, "미확인"),
    ("잘 모르겠음", None, None, None, "미확인"),
    ("배우자 휴직수당 있음", None, None, None, "미확인"),
    ("55", 550_000, 550_000, 550_000, "기재"),
    ("60 정도", 600_000, 600_000, 600_000, "추정"),
    ("1,500쯤", 15_000_000, 15_000_000, 15_000_000, "추정"),
    ("3만 얼마", 30_000, 30_000, 30_000, "추정"),
    ("7~8", 75_000, 70_000, 80_000, "추정"),
    ("월 350~390 정도야간수당 따라 달라요", 3_700_000, 3_500_000, 3_900_000, "추정"),
    ("카드값 120~160", 1_400_000, 1_200_000, 1_600_000, "추정"),
    ("1억 8천 정도", 180_000_000, 180_000_000, 180_000_000, "추정"),
    ("1,833.33만원2026 매출 22,000만원 유지 가정", 18_333_300, 18_333_300, 18_333_300, "기재"),
])
def test_parse_cell(text, won, low, high, status):
    cell = intake.parse_cell(text)
    assert (cell.won, cell.low, cell.high, cell.status) == (won, low, high, status)


def test_region_resolution():
    assert resolve_region("인천 부평구")[0] == "28237"
    assert resolve_region("대전 서구")[0] == "30170"
    assert resolve_region("경기도 성남시 중원구 가상로 10")[0] == "41133"
    code, _, _, note = resolve_region("수원")
    assert code == "4111" and "구를 몰라" in note
    assert resolve_region("어딘가")[0] is None


def test_invalid_hwp_is_reported(tmp_path):
    (tmp_path / "a.hwp").write_bytes(b"not an ole file")
    with pytest.raises(HwpError):
        hwp_tables(tmp_path / "a.hwp")
    with pytest.raises(intake.DocumentUnavailable):
        intake.read_cashflow_doc(tmp_path / "a.hwp")


@pytest.mark.skipif(not KIM_HWP.exists(), reason="원본 문서 없음")
def test_hwp_section7_matches_stored_profile():
    doc = intake.read_cashflow_doc(KIM_HWP)
    p = load_case(ROOT / "customer" / "cases" / "kim_doyun.json")
    result = intake.reconcile_cashflow(doc, p["cashflow_items"], [r for r in p["income_items"] if r["in_cashflow"]])
    assert result["status"] == "일치", result["differences"]
    assert result["rows_checked"] == 22 and result["rows_blank"] == 0


@pytest.fixture(scope="module")
def cases():
    return {key: build_case(f"t_{key}", BASE / name, ROOT) for key, name in FOLDERS.items()} if BASE.exists() else {}


def _row(case, item, section=None):
    return next(r for r in case["cashflow_items"] if r["item"].startswith(item) and (section is None or r["section"] == section))


@need_blank
def test_blank_cells_stay_unknown_not_zero(cases):
    row = _row(cases["minji"], "통신비")
    assert row["monthly_won"] is None and row["status"] == "미확인"


@need_blank
def test_range_keeps_bounds_and_midpoint(cases):
    row = _row(cases["minji"], "생활비")
    assert (row["monthly_won"], row["low_won"], row["high_won"], row["status"]) == (1_400_000, 1_200_000, 1_600_000, "추정")
    assert "카드값" in row["detail"] and "통신비가 같이" in row["detail"]


@need_blank
def test_section6_fills_blank_section7_as_estimate(cases):
    row = _row(cases["minji"], "예/적금 ①")
    assert row["monthly_won"] == 600_000 and row["status"] == "추정" and "6번" in row["assumptions"]
    assert _row(cases["minji"], "보장성보험")["monthly_won"] == 80_000  # 메모의 '합쳐서 8만 얼마'
    assert _row(cases["junho"], "보장성보험")["monthly_won"] == 240_000


@need_blank
def test_savings_type_insurance_is_left_out(cases):
    assert _row(cases["seoyeon"], "보장성보험")["monthly_won"] == 30_000  # 실비만. '저축보험?' 20쯤 제외


@need_blank
def test_insurance_not_filled_when_memo_warns_of_double_count(cases):
    row = _row(cases["hyunwoo"], "보장성보험")
    assert row["monthly_won"] is None and "중복" in row["assumptions"]


@need_blank
def test_birth_date_comes_from_receipt_when_blank(cases):
    fact = cases["minji"]["facts"]["birth_date"]
    assert fact["value"] == "1997-08-12" and fact["status"] == "추정"
    assert cases["hyunwoo"]["facts"]["birth_date"] == {"value": "1987-02-09", "status": "기재", "source_ref": "1. 기본정보"}


@need_blank
def test_household_and_housing_facts(cases):
    assert cases["minji"]["facts"]["marital_status"]["value"] == "미혼"
    assert cases["minji"]["facts"]["household_size"]["value"] == 1
    assert cases["hyunwoo"]["facts"]["marital_status"]["value"] == "기혼"
    assert cases["hyunwoo"]["facts"]["household_size"]["value"] == 3
    assert cases["hyunwoo"]["facts"]["home_owner"]["value"] is True
    assert cases["junho"]["facts"]["home_owner"]["value"] is False
    assert cases["junho"]["facts"]["household_size"]["value"] is None


@need_blank
def test_rent_is_not_applicable_for_jeonse_and_owner(cases):
    for key in ("junho", "hyunwoo"):
        row = _row(cases[key], "월 임대료")
        assert (row["monthly_won"], row["status"]) == (0, "해당없음")
    assert _row(cases["minji"], "월 임대료")["monthly_won"] == 550_000


@need_blank
def test_irregular_side_income_is_not_monthly_income(cases):
    other = next(r for r in cases["junho"]["income_items"] if r["basis"] == "기타소득")
    assert other["amount_won"] is None and "부정기" in other["assumptions"]
    kinds = {r["kind"] for r in cases["junho"]["income_items"]}
    assert {"근로", "사업"} <= kinds  # 겸업으로 분류


@need_blank
def test_multi_employer_receipt_uses_total_column():
    fields = intake.parse_withholding(BASE / "01_김민지" / "02_근로소득_원천징수영수증.docx")
    assert fields["급여"]["value"] == 44_400_000 and fields["상여"]["value"] == 3_600_000
    assert all(c["ok"] for c in intake.withholding_arithmetic(fields))


@need_blank
def test_business_receipt():
    path = BASE / "03_이서연" / "02_사업소득_원천징수영수증.docx"
    kind, fields = intake.read_withholding(path)
    assert kind == "사업" and fields["지급총액"]["value"] == 132_000_000
    assert (fields["월최소"]["value"], fields["월최대"]["value"]) == (8_000_000, 14_000_000)
    assert intake.reconcile_business(fields)["status"] == "일치"


@need_blank
def test_income_only_in_receipt_is_recorded(cases):
    gross = next(r for r in cases["minji"]["income_items"] if r["basis"] == "총급여")
    assert gross["amount_won"] == 48_000_000 and gross["period"] == "연"


@pytest.fixture
def blank_input(tmp_path, cases):
    def _make(key, lines=()):
        path = tmp_path / f"{key}.json"
        path.write_text(json.dumps(cases[key], ensure_ascii=False), encoding="utf-8")
        profile = load_case(path)
        assert not [i for i in validate(profile) if i["level"] == "error"]
        conn = connect(tmp_path / f"{key}.db")
        store(conn, profile)
        conn.execute("INSERT INTO module_results VALUES (?,?,?,?,?,?)",
                     (profile["case_id"], profile["profile_version"], "policy_to_spending", "completed",
                      json.dumps(handoff_for(profile["case_id"], profile["profile_version"], lines), ensure_ascii=False), "t"))
        conn.commit()
        return load_input(conn, profile["case_id"], profile["profile_version"])
    return _make


@need_blank
def test_incomplete_customer_gets_no_invented_effect(blank_input):
    inp = blank_input("minji")
    analysis = analyze_rows(inp)
    calc = calculate(inp, analysis)
    totals = calc["cashflow"]["totals"]
    assert totals["complete"] is False and totals["known_rows"] == 5 and totals["unknown_rows"] == 14
    assert totals["outflow_before_won"] == 2_730_000  # 60 + 10 + 8 + 55 + 140
    assert totals["unexplained_won"] == 970_000 and totals["unexplained_range_won"] == [570_000, 1_370_000]
    assert calc["effect"]["monthly_freed_won"] == 0 and analysis.recommendations == []
    assert all(m["before"] is None for m in calc["metrics"] if m["code"] in ("R01", "저축률", "소비", "R02"))
    blank = next(r for b in calc["cashflow"]["blocks"] for r in b["rows"] if r["label"] == "통신비")
    assert blank["before_won"] is None and blank["after_won"] is None
    assert "통신비" in analysis.questions[1] and "성과금" in analysis.questions[0]


@need_blank
def test_card_lump_is_conditional_only(blank_input):
    analysis = analyze_rows(blank_input("minji"))
    option = analysis.conditional[0]
    assert option["monthly_range_won"] == [120_000, 160_000] and option["dependencies"] == []
    mixed = analyze_rows(blank_input("seoyeon"))
    assert "세금 재계산" in mixed.conditional[0]["dependencies"][0]
    assert any("생활비와 업무비" in q for q in mixed.questions)


@need_blank
def test_spending_above_income_is_flagged(blank_input):
    inp = blank_input("hyunwoo")
    report, _ = build_report(inp, NoLLM(), root=ROOT)
    assert report["calculations"]["cashflow"]["totals"]["unexplained_won"] == -200_000
    assert "수입 추정치보다" in report["narrative"]["summary"]["text"]


@need_blank
def test_bonus_in_receipt_is_asked_about(blank_input):
    report, _ = build_report(blank_input("minji"), NoLLM(), root=ROOT)
    assert any("상여 360만원" in q for q in report["questions"])


@need_blank
@pytest.mark.parametrize("key", list(FOLDERS))
def test_blank_customers_produce_a_report(blank_input, key):
    report, _ = build_report(blank_input(key), NoLLM(), root=ROOT)
    html = to_html(report)
    assert report["status"] == "partial"
    assert {d["status"] for d in report["evidence"]["documents"].values()} == {"일치"}
    assert "미확인" in html and "0원으로 계산하지 않았습니다" in html
    assert report["limitations"][0].startswith("지출 금액 일부가 비어")


def test_estimated_amount_is_not_cut(make_input):
    def fuzzy(profile):
        row = next(r for r in profile["cashflow_items"] if r["item"] == "문화생활비")
        row.update(status="추정", low_won=150_000, high_won=250_000)
    analysis = analyze_rows(make_input("kim_doyun", mutate=fuzzy))
    assert not any(r["target"] == "문화생활비" for r in analysis.recommendations)


def test_policy_on_blank_expense_asks_for_the_amount():
    effect = estimate_effect({"discount_pct": 30}, {"item": "교통비", "monthly_won": None, "category": "교통비", "detail": None})
    assert effect["kind"] == "지출미확인" and "monthly_saving_won" not in effect


def test_blank_expense_line_keeps_its_policy():
    result = {"case_id": "t", "profile_version": "v1", "generated_at": "x", "questions": [],
              "search_scope": {"policy_db_as_of": "20261104"}, "other_findings": [],
              "findings": [{"policy_id": "p", "policy_name": "K-패스", "verdict": "입력상 충족", "conditions": [],
                            "benefit": {"type": "할인·환급", "summary": "s", "monthly_won": None, "duration_months": None, "total_won": None,
                                        "range_won": None, "loan_limit_won": None, "interest_support_pct": None, "discount_pct": 30.0,
                                        "discount_won": None, "match_ratio": None, "own_saving_monthly_won": None},
                            "apply": {"apply_end_date": None, "recruit_status": "always", "url": "u"}}]}
    rows = [{"section": "고정지출", "item": "교통비", "monthly_won": None, "purpose": None, "necessity": None, "detail": None}]
    line = build_handoff(result, rows, [])["expense_lines"][0]
    assert line["policy_offsets"][0]["effect"]["kind"] == "지출미확인"
    assert line["scenario_monthly_saving_won"] == 0


def test_cell_text():
    assert cell_text({"before_won": None}) == "미확인"
    assert cell_text({"before_won": 0, "not_applicable": True}) == "해당없음"
    assert cell_text({"before_won": 1_400_000, "low_won": 1_200_000, "high_won": 1_600_000}) == "120~160"
    assert cell_text({"before_won": 600_000, "estimated": True}) == "≈60"
    assert cell_text({"before_won": 650_000}) == "65"
