from pathlib import Path

import pytest

from planning import pdf
from planning.pipeline import POLICY_DB, ROOT, main, out_paths

KIM_STEP2 = ROOT / "planning" / "cases" / "kim_doyun_step2.json"


def has_browser() -> bool:
    try:
        pdf.find_browser()
        return True
    except pdf.PdfError:
        return False


def test_printable_opens_every_card_and_sets_page():
    html = '<html><head></head><body><details class="card"><summary>a</summary></details><details open>b</details></body></html>'
    out = pdf.printable(html)
    assert out.count("<details open") == 2 and "<details open open" not in out
    assert "@page" in out.split("</head>")[0]


def test_out_paths_by_format(tmp_path):
    assert out_paths(tmp_path, "c", "certain")["dashboard"].suffix == ".html"  # 기존 호출 그대로
    only_pdf = out_paths(tmp_path, "c", "certain", "", "pdf")
    assert "dashboard" not in only_pdf and only_pdf["dashboard_pdf"].name == "dashboard_c_certain_v1.pdf"
    assert {p.suffix for p in out_paths(tmp_path, "c", "certain", "_x", "both").values()} == {".json", ".html", ".pdf"}


def test_find_browser_env(tmp_path, monkeypatch):
    fake = tmp_path / "browser.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("PDF_BROWSER", str(fake))
    assert pdf.find_browser() == fake
    monkeypatch.setenv("PDF_BROWSER", str(tmp_path / "none.exe"))
    with pytest.raises(pdf.PdfError):
        pdf.find_browser()


@pytest.mark.skipif(not POLICY_DB.exists(), reason="정책 DB 없음")
def test_missing_browser_stops_before_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("PDF_BROWSER", str(tmp_path / "none.exe"))
    out = tmp_path / "out"
    assert main(["--case", "kim_doyun", "--step2", str(KIM_STEP2), "--out-dir", str(out)]) == 1
    assert "PDF 변환" in capsys.readouterr().err
    assert not out.exists() or not list(out.iterdir())


@pytest.mark.skipif(not POLICY_DB.exists() or not has_browser(), reason="정책 DB 또는 브라우저 없음")
def test_main_writes_pdf_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    assert main(["--case", "kim_doyun", "--step2", str(KIM_STEP2), "--out-dir", str(tmp_path)]) == 0
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["ai_log_plan_kim_doyun_certain_v1.json", "dashboard_kim_doyun_certain_v1.pdf",
                     "plan_kim_doyun_certain_v1.json"]
    data = (tmp_path / names[1]).read_bytes()
    assert data[:5] == b"%PDF-" and len(data) > 20_000
