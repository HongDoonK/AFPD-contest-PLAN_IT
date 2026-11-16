from pathlib import Path

import pytest

from customer.profile import load_case
from spending_report import intake

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "가상고객데이터"
CASES = ROOT / "customer" / "cases"
RECEIPT = DOCS / "02_근로소득원천징수영수증_가상학습용.docx"
LEE_FF = DOCS / "가상 고객 설정2.docx"
need_docs = pytest.mark.skipif(not RECEIPT.exists() or not LEE_FF.exists(), reason="원본 문서 없음")


def _kim_doc():
    lines = (Path(__file__).parent / "fixtures" / "ff_kim_section7.txt").read_text(encoding="utf-8").splitlines()
    return intake.parse_section7_lines(lines)


def _stored(case):
    p = load_case(CASES / f"{case}.json")
    return p["cashflow_items"], [r for r in p["income_items"] if r["in_cashflow"]], p


def test_pdf_text_section7():
    doc = _kim_doc()
    assert [cell.won for _, cell in doc.income] == [4_280_000, 0, 0]
    assert len(doc.outflow) == 19
    amounts = {(block, label): cell.won for block, label, cell in doc.outflow}
    assert amounts[("고정지출", "교통비")] == 100_000
    assert amounts[("변동지출", "기타")] == 400_000
    assert (doc.total_income.won, doc.total_outflow.won) == (4_280_000, 4_280_000)
    assert (doc.unverified_income.won, doc.unverified_outflow.won) == (0, 0)


def test_pdf_text_matches_stored_profile():
    rows, income, _ = _stored("kim_doyun")
    result = intake.reconcile_cashflow(_kim_doc(), rows, income)
    assert result["status"] == "일치", result["differences"]


def test_changed_value_and_missing_row_are_reported():
    rows, income, _ = _stored("kim_doyun")
    next(r for r in rows if r["item"] == "통신비")["monthly_won"] = 90_000
    rows = [r for r in rows if r["item"] != "주식"]
    result = intake.reconcile_cashflow(_kim_doc(), rows, income)
    assert result["status"] == "불일치"
    assert any("통신비" in d for d in result["differences"])
    assert any("주식" in d for d in result["differences"])


@need_docs
def test_docx_section7_matches_stored_profile():
    doc = intake.read_cashflow_doc(LEE_FF)
    assert doc.total_income.won == 18_333_300
    assert {(b, l): c.won for b, l, c in doc.outflow}[("고정지출", "종합·지방세 월 적립")] == 5_051_750
    rows, income, _ = _stored("lee_seojun")
    result = intake.reconcile_cashflow(doc, rows, income)
    assert result["status"] == "일치", result["differences"]


@need_docs
def test_withholding_fields_and_classification():
    fields = intake.parse_withholding(RECEIPT)
    assert fields["총급여"] == {"value": 57_600_000, "category": "소득공제", "source_ref": "㉑ 총급여 ⑯"}
    assert fields["결정세액_지방소득세"]["value"] == 206_590
    assert fields["차감징수_소득세"]["value"] == -334_020
    assert fields["실효세율"]["value"] == 3.6
    assert fields["신용카드공제"]["value"] == 1_440_000
    assert all(c["ok"] for c in intake.withholding_arithmetic(fields))


@need_docs
def test_withholding_reconcile():
    _, _, p = _stored("kim_doyun")
    fields = intake.parse_withholding(RECEIPT)
    assert intake.reconcile_withholding(fields, p["tax_items"], p["income_items"])["status"] == "일치"
    next(t for t in p["tax_items"] if t["item"] == "과세표준")["amount_won"] = 1
    result = intake.reconcile_withholding(fields, p["tax_items"], p["income_items"])
    assert result["status"] == "불일치" and any("과세표준" in d for d in result["differences"])


def test_withholding_arithmetic_catches_wrong_document():
    keys = {"급여": 100, "상여": 0, "총급여": 100, "근로소득공제": 10, "근로소득금액": 90, "기본공제": 0, "국민연금": 0,
            "특별소득공제계": 0, "차감소득금액": 90, "그밖의소득공제계": 0, "과세표준": 90, "산출세액": 9, "세액감면계": 0,
            "세액공제계": 1, "결정세액": 7, "결정세액_소득세": 7, "종전납부_소득세": 0, "기납부_소득세": 7, "납부특례_소득세": 0,
            "차감징수_소득세": 0}
    checks = intake.withholding_arithmetic({k: {"value": v} for k, v in keys.items()})
    assert [c["ok"] for c in checks] == [True, True, True, True, False, True]


def test_unreadable_documents_are_reported_not_raised_as_crash(tmp_path, monkeypatch):
    with pytest.raises(intake.DocumentUnavailable):
        intake.read_cashflow_doc(tmp_path / "없는파일.docx")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(intake.DocumentUnavailable):
        intake.read_cashflow_doc(tmp_path / "a.txt")
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    monkeypatch.setitem(__import__("sys").modules, "pdfplumber", None)
    with pytest.raises(intake.DocumentUnavailable):
        intake.read_cashflow_doc(tmp_path / "a.pdf")
