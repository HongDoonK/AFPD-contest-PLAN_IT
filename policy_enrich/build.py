"""원본 youth_policy.db를 복사해 (가)~(다) 테이블을 채운 보강 DB를 만든다.

사용법:
    python -m policy_enrich.build --source <youth_policy.db> --out data/youth_policy_enriched.db [--as-of 20261104]

FINLIFE_API_KEY가 있으면 금감원 예금·적금·연금저축도 함께 수집한다.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from . import bokjiro, finlife
from .benefits import build_benefit
from .conditions import build_condition
from .reference import ASSET_PRODUCTS, MEDIAN_INCOME_MONTHLY, MEDIAN_INCOME_YEAR

SCHEMA = Path(__file__).with_name("schema.sql")
DERIVED_TABLES = ["policy_conditions", "policy_benefits", "asset_products", "median_income",
                  "fin_product_options", "fin_loan_options", "fin_products"]


def _insert(conn, table, rows):
    if not rows:
        return
    cols = list(rows[0])
    conn.executemany(f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [tuple(r[c] for c in cols) for r in rows])


def _migrate_small_loans(conn, collected_at):
    """smallloan_youth(서민금융 청년 대출 21건)를 fin_products 형식으로 옮긴다."""
    rows = []
    for r in conn.execute("SELECT * FROM smallloan_youth"):
        item = dict(zip([d[0] for d in conn.execute("SELECT * FROM smallloan_youth LIMIT 0").description], r))
        rows.append({
            "product_key": f"youth_loan:{item.get('ofrInstNm') or ''}:{item.get('snq') or item.get('id')}",
            "product_type": "youth_loan", "top_fin_grp_no": None, "fin_co_no": None,
            "kor_co_nm": item.get("ofrInstNm"), "fin_prdt_cd": str(item.get("snq") or item.get("id")),
            "fin_prdt_nm": item.get("finPrdNm"), "join_way": item.get("jnMthd"),
            "join_member": item.get("trgt"), "join_deny": None, "max_limit_won": None,
            "spcl_cnd": item.get("prftAddIrtCond"), "mtrt_int": None,
            "etc_note": f"한도 {item.get('lnLmt')} / 금리 {item.get('irt')} / 소득 {item.get('incm')} / 용도 {item.get('usge')}",
            "dcls_month": item.get("basYm"), "dcls_strt_day": None, "dcls_end_day": None,
            "collected_at": item.get("collected_at") or collected_at,
            "raw_json": json.dumps(item, ensure_ascii=False, default=str),
        })
    _insert(conn, "fin_products", rows)
    return len(rows)


def build(source: Path, out: Path, as_of: str) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.resolve() != source.resolve():
        shutil.copyfile(source, out)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    for view in ("v_policy_finance", "v_fin_product_best_rate", "v_fin_loan_rate"):
        conn.execute(f"DROP VIEW IF EXISTS {view}")
    for table in DERIVED_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    # 원본 DB에 남아 있던 실사용 프로필은 보강 DB로 넘기지 않는다
    conn.execute("DELETE FROM users")

    policies = [dict(r) for r in conn.execute("SELECT * FROM policies")]
    conditions = []
    for row in policies:
        c = build_condition(row, as_of).as_row()
        c["as_of"] = as_of
        conditions.append(c)
    benefits = [build_benefit(row).as_row() for row in policies]
    _insert(conn, "policy_conditions", conditions)
    _insert(conn, "policy_benefits", benefits)

    _insert(conn, "asset_products", ASSET_PRODUCTS)
    _insert(conn, "median_income", [{"year": MEDIAN_INCOME_YEAR, "household_size": k, "monthly_won": v}
                                    for k, v in MEDIAN_INCOME_MONTHLY.items()])

    loans = _migrate_small_loans(conn, as_of)
    finlife_result = finlife.collect(conn)
    conn.commit()
    # 복지로는 상세 호출이 수천 건이다. 키가 있을 때만 받고, 한도에 걸리면 policy_enrich.bokjiro로 이어 받는다
    bokjiro_result = bokjiro.collect(conn)
    conn.commit()

    report = {
        "policies": len(policies),
        "recruit_status": dict(Counter(c["recruit_status"] for c in conditions)),
        "income_basis": dict(Counter(c["income_basis"] for c in conditions)),
        "condition_confidence": dict(Counter(c["parse_confidence"] for c in conditions)),
        "benefit_type": dict(Counter(b["benefit_type"] for b in benefits)),
        "benefit_confidence": dict(Counter(b["parse_confidence"] for b in benefits)),
        "dedup_groups": len({c["dedup_key"] for c in conditions}),
        "asset_products": len(ASSET_PRODUCTS),
        "youth_loans": loans,
        "finlife": finlife_result,
        "bokjiro": bokjiro_result,
    }
    conn.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", default=Path("data/youth_policy_enriched.db"), type=Path)
    parser.add_argument("--as-of", default=date.today().strftime("%Y%m%d"))
    args = parser.parse_args()
    report = build(args.source, args.out, args.as_of)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
