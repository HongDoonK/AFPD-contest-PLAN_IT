"""케이스 JSON을 검증해 customer_profiles.db에 넣고, 정책 판정용 값을 보여준다.

사용법:
    python -m customer.load customer/cases/*.json [--db data/customer_profiles.db] [--policy-db data/youth_policy_enriched.db]

검증에서 error가 나온 케이스는 저장하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .profile import connect, load_case, policy_inputs, store, validate


def _median_table(policy_db: Path) -> dict[int, int] | None:
    if not policy_db.exists():
        return None
    with sqlite3.connect(policy_db) as conn:
        return dict(conn.execute("SELECT household_size, monthly_won FROM median_income"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="+", type=Path)
    parser.add_argument("--db", default=Path("data/customer_profiles.db"), type=Path)
    parser.add_argument("--policy-db", default=Path("data/youth_policy_enriched.db"), type=Path)
    args = parser.parse_args()

    median = _median_table(args.policy_db)
    conn = connect(args.db)
    for path in args.cases:
        profile = load_case(path)
        issues = validate(profile)
        errors = [i for i in issues if i["level"] == "error"]
        print(f"\n## {profile['case_id']} {profile['profile_version']} ({path.name})")
        for i in issues:
            print(f"  [{i['level']}] {i['check']}: {i['message']}")
        if errors:
            print("  → 저장하지 않음")
            continue
        store(conn, profile)
        print("  → 저장 완료")
        print(json.dumps(policy_inputs(profile, median_monthly=median), ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
