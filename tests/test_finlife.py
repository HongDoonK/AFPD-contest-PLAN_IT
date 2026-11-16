import sqlite3
from pathlib import Path

from policy_enrich import finlife

PAGE = {
    "_top_fin_grp": "020000", "max_page_no": 1, "err_cd": "000",
    "baseList": [{"dcls_month": "202610", "fin_co_no": "0010001", "fin_prdt_cd": "WR0001",
                  "kor_co_nm": "우리은행", "fin_prdt_nm": "WON적금", "join_way": "스마트폰",
                  "join_deny": "1", "join_member": "실명의 개인", "max_limit": "500000",
                  "spcl_cnd": "급여이체 0.3%p", "mtrt_int": "만기 후 1년 이내 50%", "etc_note": ""}],
    "optionList": [{"fin_co_no": "0010001", "fin_prdt_cd": "WR0001", "intr_rate_type_nm": "단리",
                    "rsrv_type_nm": "자유적립식", "save_trm": "12", "intr_rate": 3.1, "intr_rate2": 4.2},
                   {"fin_co_no": "0010001", "fin_prdt_cd": "WR0001", "intr_rate_type_nm": "단리",
                    "rsrv_type_nm": "자유적립식", "save_trm": "24", "intr_rate": 3.0, "intr_rate2": None}],
}


def test_normalize_and_store():
    products, options = finlife.normalize("saving", [PAGE], "2026-11-04")
    assert products[0]["product_key"] == "saving:0010001:WR0001"
    assert products[0]["max_limit_won"] == 500_000
    assert [o["save_trm_months"] for o in options] == [12, 24]
    assert options[1]["intr_rate2"] is None

    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE policies(policy_id TEXT);")
    conn.executescript((Path(finlife.__file__).with_name("schema.sql")).read_text(encoding="utf-8"))
    finlife.store(conn, products, options)
    finlife.store(conn, products, options)  # 재수집해도 옵션이 중복되지 않는다
    assert conn.execute("SELECT COUNT(*) FROM fin_product_options").fetchone()[0] == 2
    best = conn.execute("SELECT max_rate FROM v_fin_product_best_rate WHERE save_trm_months=12").fetchone()[0]
    assert best == 4.2


def _schema_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE policies(policy_id TEXT);")
    conn.executescript((Path(finlife.__file__).with_name("schema.sql")).read_text(encoding="utf-8"))
    return conn


def test_rent_loan_options_go_to_loan_table():
    page = {"_top_fin_grp": "020000",
            "baseList": [{"fin_co_no": "0010001", "fin_prdt_cd": "10561", "kor_co_nm": "우리은행",
                          "fin_prdt_nm": "우리전세론", "loan_lmt": "최대4.44억원", "erly_rpay_fee": "0.76%"}],
            "optionList": [{"fin_co_no": "0010001", "fin_prdt_cd": "10561", "rpay_type_nm": "분할상환방식",
                            "lend_rate_type_nm": "변동금리", "lend_rate_min": 4.04, "lend_rate_max": 4.74,
                            "lend_rate_avg": None}]}
    products, options = finlife.normalize("rent_loan", [page], "2026-11-04")
    assert products[0]["loan_limit_text"] == "최대4.44억원"
    conn = _schema_conn()
    finlife.store(conn, products, options)
    assert conn.execute("SELECT COUNT(*) FROM fin_product_options").fetchone()[0] == 0
    assert conn.execute("SELECT rate_min, rate_max FROM v_fin_loan_rate").fetchone() == (4.04, 4.74)


def test_credit_loan_spreads_grades_and_keeps_types_apart():
    base = {"fin_co_no": "0010001", "fin_prdt_cd": "CR0001A", "kor_co_nm": "우리은행", "fin_prdt_nm": "신용대출"}
    page = {"_top_fin_grp": "020000",
            "baseList": [{**base, "crdt_prdt_type": "1", "crdt_prdt_type_nm": "일반신용대출"},
                         {**base, "crdt_prdt_type": "2", "crdt_prdt_type_nm": "마이너스한도대출"}],
            "optionList": [{"fin_co_no": "0010001", "fin_prdt_cd": "CR0001A", "crdt_prdt_type": "1",
                            "crdt_lend_rate_type_nm": "대출금리", "crdt_grad_1": 6.03, "crdt_grad_13": 9.12,
                            "crdt_grad_avg": 6.62},
                           {"fin_co_no": "0010001", "fin_prdt_cd": "CR0001A", "crdt_prdt_type": "1",
                            "crdt_lend_rate_type_nm": "기준금리", "crdt_grad_1": 2.8}]}
    products, options = finlife.normalize("credit_loan", [page], "2026-11-04")
    assert len({p["product_key"] for p in products}) == 2
    conn = _schema_conn()
    finlife.store(conn, products, options)
    rows = conn.execute("SELECT credit_grade, rate_avg FROM v_fin_loan_rate ORDER BY rate_avg").fetchall()
    assert rows == [("900점 초과", 6.03), ("평균", 6.62), ("300점 이하", 9.12)]  # 기준금리 행은 빠진다


def test_collect_without_key_is_skipped(monkeypatch):
    monkeypatch.delenv("FINLIFE_API_KEY", raising=False)
    assert "skipped" in finlife.collect(sqlite3.connect(":memory:"))
