"""내려받은 뒤 한 번 실행하는 준비 스크립트.

    python -X utf8 setup_data.py

1. data/youth_policy_enriched.db.gz 를 풀어 정책 DB를 만든다.
2. 고객 프로필 DB(data/customer_profiles.db)를 만들고 가상 고객 6명을 넣는다.
3. 고객별 정책 매칭 결과와 소비 보고서(output/policy_*.json, spending_report_*)를 만든다.

API 키는 필요 없다. 이미 있는 파일은 건드리지 않는다(--force로 다시 만든다).
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POLICY_GZ = ROOT / "data" / "youth_policy_enriched.db.gz"
POLICY_DB = ROOT / "data" / "youth_policy_enriched.db"
CUSTOMER_DB = ROOT / "data" / "customer_profiles.db"
CASES = ["customer/cases/kim_doyun.json", "customer/cases/lee_seojun.json"]
BLANKS = {"blank_01": "01_김민지", "blank_02": "02_박준호", "blank_03": "03_이서연", "blank_04": "04_김현우"}


def run(*args: str) -> None:
    print("  $ python -m", " ".join(args))
    subprocess.run([sys.executable, "-X", "utf8", "-m", *args], cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="이미 있는 DB도 다시 만든다")
    args = parser.parse_args()

    print("[1/3] 정책 DB 압축 풀기")
    if POLICY_DB.exists() and not args.force:
        print("  이미 있음:", POLICY_DB.relative_to(ROOT))
    else:
        with gzip.open(POLICY_GZ, "rb") as src, open(POLICY_DB, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        print(f"  만들었음: {POLICY_DB.relative_to(ROOT)} ({POLICY_DB.stat().st_size / 1e6:.0f}MB)")

    print("[2/3] 고객 프로필 DB")
    if CUSTOMER_DB.exists() and not args.force:
        print("  이미 있음:", CUSTOMER_DB.relative_to(ROOT))
    else:
        CUSTOMER_DB.unlink(missing_ok=True)
        run("customer.load", *CASES)
        for case_id, folder in BLANKS.items():
            run("customer.from_documents", f"가상고객데이터/빈칸형_가상고객_4명/{folder}", "--case-id", case_id)

    print("[3/3] 정책 매칭 → 지출 연결 → 소비 보고서")
    (ROOT / "output").mkdir(exist_ok=True)
    for case_id in ["kim_doyun", "lee_seojun", *BLANKS]:
        run("customer.policy_match", case_id)
        run("customer.spending_handoff", case_id)
        run("spending_report", case_id)

    print("\n준비 끝. 다음으로 실행:")
    print("  python -X utf8 -m planning.pipeline --case kim_doyun --step2 planning/cases/kim_doyun_step2.json")


if __name__ == "__main__":
    main()
