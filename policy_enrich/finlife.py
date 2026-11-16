"""(다) 금융감독원 '금융상품 한눈에' 오픈 API 수집기.

정기예금·적금은 fin_products / fin_product_options에,
전세자금·개인신용·주택담보 대출은 fin_products / fin_loan_options에 넣는다.
인증키는 finlife.fss.or.kr에서 발급받아 환경변수 FINLIFE_API_KEY에 둔다.
네트워크 호출(fetch_products)과 변환(normalize)을 나눠, 변환은 키 없이 테스트한다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime

BASE_URL = "https://finlife.fss.or.kr/finlifeapi"

# product_type → API 이름
ENDPOINTS = {
    "deposit": "depositProductsSearch.json",        # 정기예금
    "saving": "savingProductsSearch.json",          # 적금
    "rent_loan": "rentHouseLoanProductsSearch.json",   # 전세자금대출
    "credit_loan": "creditLoanProductsSearch.json",    # 개인신용대출
    "mortgage_loan": "mortgageLoanProductsSearch.json",  # 주택담보대출
    # 연금저축(annuitySavingProductsSearch.json)은 2026-11-04 확인 시 대출 상품 목록을 돌려줘서 뺐다.
    # API가 정상화되면 여기와 FIN_GROUPS에 "annuity"를 다시 넣는다.
}
LOAN_TYPES = {"rent_loan", "credit_loan", "mortgage_loan"}

# 권역 코드: 은행, 저축은행
FIN_GROUPS = {product_type: ["020000", "030300"] for product_type in ENDPOINTS}

# 신용대출 금리 칸 → 신용점수 구간(KCB/NICE 공시 기준)
CREDIT_GRADES = {
    "crdt_grad_1": "900점 초과", "crdt_grad_4": "801~900점", "crdt_grad_5": "701~800점",
    "crdt_grad_6": "601~700점", "crdt_grad_10": "501~600점", "crdt_grad_11": "401~500점",
    "crdt_grad_12": "301~400점", "crdt_grad_13": "300점 이하", "crdt_grad_avg": "평균",
}


def fetch_products(product_type: str, api_key: str, sleep_sec: float = 0.2) -> list[dict]:
    """한 상품 유형의 모든 권역·페이지를 받아 API result 블록 목록으로 돌려준다."""
    pages = []
    for group in FIN_GROUPS[product_type]:
        page = 1
        while True:
            query = urllib.parse.urlencode({"auth": api_key, "topFinGrpNo": group, "pageNo": page})
            url = f"{BASE_URL}/{ENDPOINTS[product_type]}?{query}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))["result"]
            if result.get("err_cd") not in (None, "000"):
                raise RuntimeError(f"finlife {product_type}/{group}: {result.get('err_cd')} {result.get('err_msg')}")
            result["_top_fin_grp"] = group
            pages.append(result)
            if page >= int(result.get("max_page_no") or 1):
                break
            page += 1
            time.sleep(sleep_sec)
    return pages


def _key(product_type: str, item: dict) -> str:
    key = f"{product_type}:{item.get('fin_co_no')}:{item.get('fin_prdt_cd')}"
    # 신용대출은 같은 상품코드에 일반신용·마이너스통장이 따로 공시된다
    if item.get("crdt_prdt_type"):
        key += f":{item['crdt_prdt_type']}"
    return key


def _num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _one_line(value):
    return "".join(str(value).split("\n")).strip() if value else value


def _product_row(product_type: str, group: str, item: dict, collected_at: str) -> dict:
    return {
        "product_key": _key(product_type, item),
        "product_type": product_type,
        "top_fin_grp_no": group,
        "fin_co_no": item.get("fin_co_no"),
        "kor_co_nm": _one_line(item.get("kor_co_nm")),
        "fin_prdt_cd": item.get("fin_prdt_cd"),
        "fin_prdt_nm": _one_line(item.get("fin_prdt_nm")),  # 원문에 "일반신용\n대출"처럼 줄바꿈이 섞여 온다
        "join_way": item.get("join_way"),
        "join_member": item.get("join_member"),
        "join_deny": item.get("join_deny"),  # 1 제한없음, 2 서민전용, 3 일부제한
        "max_limit_won": _num(item.get("max_limit")),
        "spcl_cnd": item.get("spcl_cnd"),
        "mtrt_int": item.get("mtrt_int"),
        "etc_note": item.get("etc_note"),
        # 대출 전용 칸(예적금은 비어 있다)
        "loan_type_nm": item.get("crdt_prdt_type_nm"),
        "loan_limit_text": item.get("loan_lmt"),
        "erly_rpay_fee": item.get("erly_rpay_fee"),
        "dly_rate": item.get("dly_rate"),
        "loan_inci_expn": item.get("loan_inci_expn"),
        "cb_name": item.get("cb_name"),
        "dcls_month": item.get("dcls_month"),
        "dcls_strt_day": item.get("dcls_strt_day"),
        "dcls_end_day": item.get("dcls_end_day"),
        "collected_at": collected_at,
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


def _saving_option_row(product_type: str, opt: dict) -> dict:
    return {
        "product_key": _key(product_type, opt),
        "intr_rate_type_nm": opt.get("intr_rate_type_nm"),  # 단리/복리
        "rsrv_type_nm": opt.get("rsrv_type_nm"),            # 적금: 정액적립식/자유적립식
        "save_trm_months": int(opt["save_trm"]) if str(opt.get("save_trm") or "").isdigit() else None,
        "intr_rate": _num(opt.get("intr_rate")),      # 기본금리
        "intr_rate2": _num(opt.get("intr_rate2")),    # 최고 우대금리
        "raw_json": json.dumps(opt, ensure_ascii=False),
    }


def _loan_option_rows(product_type: str, opt: dict) -> list[dict]:
    """대출 옵션 → 금리 행. 신용대출은 신용점수 구간마다 한 행으로 편다."""
    base = {
        "product_key": _key(product_type, opt),
        "mrtg_type_nm": opt.get("mrtg_type_nm"),          # 주담대: 아파트/아파트외
        "rpay_type_nm": opt.get("rpay_type_nm"),          # 분할상환/만기일시상환
        "lend_rate_type_nm": opt.get("lend_rate_type_nm") or opt.get("crdt_lend_rate_type_nm"),
        "raw_json": json.dumps(opt, ensure_ascii=False),
    }
    if product_type != "credit_loan":
        return [{**base, "credit_grade": None,
                 "rate_min": _num(opt.get("lend_rate_min")), "rate_max": _num(opt.get("lend_rate_max")),
                 "rate_avg": _num(opt.get("lend_rate_avg"))}]
    rows = []
    for field, label in CREDIT_GRADES.items():
        rate = _num(opt.get(field))
        if rate is not None:
            rows.append({**base, "credit_grade": label, "rate_min": None, "rate_max": None, "rate_avg": rate})
    return rows


def normalize(product_type: str, pages: list[dict], collected_at: str) -> tuple[list[dict], list[dict]]:
    """API result 블록들 → (상품 행, 옵션 행). 옵션 행 형식은 예적금/대출에 따라 다르다."""
    products, options = [], []
    for page in pages:
        group = page.get("_top_fin_grp", "")
        for item in page.get("baseList", []):
            products.append(_product_row(product_type, group, item, collected_at))
        for opt in page.get("optionList", []):
            if product_type in LOAN_TYPES:
                options.extend(_loan_option_rows(product_type, opt))
            else:
                options.append(_saving_option_row(product_type, opt))
    return products, options


def _insert(conn, table, rows):
    if rows:
        cols = list(rows[0])
        conn.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [tuple(r[c] for c in cols) for r in rows])


def store(conn: sqlite3.Connection, products: list[dict], options: list[dict]) -> None:
    keys = [(p["product_key"],) for p in products]
    conn.executemany("DELETE FROM fin_product_options WHERE product_key = ?", keys)
    conn.executemany("DELETE FROM fin_loan_options WHERE product_key = ?", keys)
    _insert(conn, "fin_products", products)
    loan = products and products[0]["product_type"] in LOAN_TYPES
    _insert(conn, "fin_loan_options" if loan else "fin_product_options", options)


def collect(conn: sqlite3.Connection, api_key: str | None = None) -> dict:
    """키가 없으면 아무것도 하지 않고 skipped를 돌려준다(빌드는 계속 진행)."""
    api_key = api_key or os.getenv("FINLIFE_API_KEY", "")
    if not api_key:
        return {"skipped": "FINLIFE_API_KEY 없음"}
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts = {}
    for product_type in ENDPOINTS:
        products, options = normalize(product_type, fetch_products(product_type, api_key), collected_at)
        store(conn, products, options)
        counts[product_type] = len(products)
    return counts
