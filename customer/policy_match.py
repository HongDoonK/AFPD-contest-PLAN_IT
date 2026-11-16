"""고객 입력(customer_profiles.db) × 보강 정책 DB → 정책 판정 결과(module_result JSON).

조립 설명서 4장(정책 확인)과 9장(결과 규격)을 따른다.
- 조건마다 충족 / 불충족 / 미확인을 따로 내고, 비교한 정책 값·고객 값·근거를 남긴다.
- 정책 판정은 '입력상 충족'이지 기관의 최종 선정이 아니다. 지원금은 확정 수입으로 넘기지 않는다.
- 소득 기준은 정책이 쓰는 지표에 맞는 고객 값만 비교한다. 근사로 비교했으면 approximate=true로 표시하고,
  기준선 ±10% 안이면 미확인으로 돌린다.

사용법:
    python -m customer.policy_match kim_doyun [--version v1] [--policy-db ...] [--db ...] [--out output/]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from .profile import age_on
from .welfare_match import match_welfare

MET, NOT_MET, UNKNOWN = "충족", "불충족", "미확인"

JOB = {"0013001": "재직자", "0013002": "자영업자", "0013003": "미취업자", "0013004": "프리랜서",
       "0013005": "일용근로자", "0013006": "(예비)창업자", "0013007": "단기근로자", "0013008": "영농종사자",
       "0013009": "기타", "0013010": "제한없음"}
SCHOOL = {"0049001": "고졸 미만", "0049002": "고교 재학", "0049003": "고졸 예정", "0049004": "고교 졸업",
          "0049005": "대학 재학", "0049006": "대졸 예정", "0049007": "대학 졸업", "0049008": "석·박사",
          "0049009": "기타", "0049010": "제한없음"}
SPECIAL = {"0014001": "중소기업", "0014002": "여성", "0014003": "기초생활수급자", "0014004": "한부모가정",
           "0014005": "장애인", "0014006": "농업인", "0014007": "군인", "0014008": "지역인재", "0014009": "기타",
           "0014010": "제한없음"}
MARRIAGE = {"0055001": "기혼", "0055002": "미혼", "0055003": "제한없음"}
# 고객 취업 상태 → 온통청년 취업 코드
EMPLOYMENT_TO_JOB = {"재직": {"0013001"}, "프리랜서": {"0013004"}, "자영업": {"0013002"}, "미취업": {"0013003"}}

# 정책명으로만 드러나는 대상 집단. (패턴, 대상명, 고객 정보로 판정하는 함수 or None)
_GROUP_RULES = [
    (r"신혼|예비부부|결혼", "신혼·예비부부", lambda c: NOT_MET if c["marital_status"] == "미혼" and "결혼" not in c["goals"] else UNKNOWN),
    (r"출산|임산부|임신|산모|산후|신생아|영아|아동|자녀|양육|육아|부모급여|다자녀|난임", "임신·출산·자녀 양육",
     lambda c: NOT_MET if c["household_size"] == 1 else UNKNOWN),
    (r"장병|병사|군복무|입영|병역|간부", "군 복무 중인 청년",
     lambda c: NOT_MET if c["employment_status"] in ("재직", "프리랜서") else UNKNOWN),
    (r"고교|고등학|특성화고|마이스터고|중학생|초등학", "초·중·고 학생",
     lambda c: NOT_MET if c["age"] is not None and c["age"] >= 20 else UNKNOWN),
    (r"대학생|학자금|장학|재학생|대학원|석박사|기숙사", "대학(원)생", None),
    (r"예술인|예술가|창작자|문화예술인|공예", "예술인", None),
    (r"농업인|영농|귀농|어업인|어촌|귀어|임업", "농·어업인", None),
    (r"한부모|자립준비|보호종료|퇴소|수급자|차상위|생계급여|주거급여|의료급여|장애인|경계선지능|북한이탈|다문화|가족돌봄|은둔|고립|자살",
     "취약계층 특화", None),
    (r"창업|스타트업|소상공인|자영업|청년기업", "창업자·소상공인",
     lambda c: UNKNOWN if c["customer_type"] in ("사업·프리랜서", "겸업") else NOT_MET),
]
# 개인에게 주는 혜택이 아닌 행정·홍보·기반 사업
_ADMIN_RE = re.compile(r"홍보|근절|실태조사|기본계획|위원회|협의체|조례|체계\s*구축|친화도시|채널\s*(?:구축|운영)|공간\s*조성|센터\s*운영|서포터즈")
_ADMIN_CATEGORY = ("정책인프라구축",)


# ── 고객 입력 읽기 ───────────────────────────────────────────
def load_customer(conn: sqlite3.Connection, case_id: str, version: str | None) -> dict:
    if version is None:
        version = conn.execute("SELECT profile_version FROM profiles WHERE case_id=? ORDER BY created_at DESC LIMIT 1",
                               (case_id,)).fetchone()[0]
    key = (case_id, version)
    as_of, ctype = conn.execute("SELECT as_of_date, customer_type FROM profiles WHERE case_id=? AND profile_version=?",
                                key).fetchone()
    facts = {}
    for field, text, num, status in conn.execute(
            "SELECT field, value_text, value_num, status FROM profile_facts WHERE case_id=? AND profile_version=?", key):
        value = num if num is not None else {"true": True, "false": False}.get(text, text)
        facts[field] = {"value": value, "status": status}
    income = {}
    for kind, basis, tax_year, amount, status in conn.execute(
            "SELECT kind, basis, tax_year, amount_won, status FROM income_items WHERE case_id=? AND profile_version=? "
            "AND period='연' ORDER BY tax_year", key):
        income[(kind, basis)] = {"amount_won": amount, "tax_year": tax_year, "status": status}
    assets = conn.execute("SELECT COALESCE(SUM(balance_won),0) FROM assets WHERE case_id=? AND profile_version=?",
                          key).fetchone()[0]
    debts = conn.execute("SELECT COALESCE(SUM(balance_won),0) FROM liabilities WHERE case_id=? AND profile_version=?",
                         key).fetchone()[0]
    goals = " ".join(n for (n,) in conn.execute("SELECT name FROM goals WHERE case_id=? AND profile_version=?", key))
    f = lambda k: facts.get(k, {}).get("value")
    birth = f("birth_date")
    return {
        "case_id": case_id, "profile_version": version, "as_of": as_of, "customer_type": ctype,
        "age": age_on(birth, as_of) if birth else None, "birth_date": birth,
        "region_code": f("region_code"), "sex": f("sex"),
        "household_size": int(f("household_size")) if f("household_size") is not None else None,
        "marital_status": f("marital_status"), "home_owner": f("home_owner"),
        "employment_status": f("employment_status"), "employer_size": f("employer_size"),
        "parents_household": f("parents_household"),
        "income": income, "net_assets_won": assets - debts, "goals": goals, "facts": facts,
    }


# ── 조건 판정 ────────────────────────────────────────────────
def _cond(name, policy_value, customer_value, result, basis="", note="", approximate=False):
    return {"condition": name, "policy_value": policy_value, "customer_value": customer_value,
            "result": result, "basis": basis, "note": note, "approximate": approximate}


def _codes(value: str | None) -> list[str]:
    return [c.strip() for c in (value or "").split(",") if c.strip()]


def _labels(codes, table):
    return [table.get(c, c) for c in codes]


def check_age(p, c):
    lo, hi = int(p["min_age"] or 0), int(p["max_age"] or 0)
    if not lo and not hi:
        return _cond("연령", "제한 없음", c["age"], MET)
    if c["age"] is None:
        return _cond("연령", f"{lo}~{hi}세", None, UNKNOWN, note="생년월일 필요")
    ok = (not lo or c["age"] >= lo) and (not hi or c["age"] <= hi)
    note = ""
    if ok and hi and c["age"] == hi:
        note = "올해가 상한 나이. 생일 이후 신청 불가일 수 있음"
    return _cond("연령", f"{lo or ''}~{hi or ''}세", f"만 {c['age']}세", MET if ok else NOT_MET,
                 basis="기준일 만 나이", note=note + (" 병역 기간 차감 여부 미확인" if not ok else ""))


def check_job(p, c):
    codes = _codes(p["job_cd"])
    if not codes or "0013010" in codes:
        return _cond("취업 상태", "제한 없음", c["employment_status"], MET)
    mine = EMPLOYMENT_TO_JOB.get(c["employment_status"] or "", set())
    labels = _labels(codes, JOB)
    if mine & set(codes):
        return _cond("취업 상태", labels, c["employment_status"], MET)
    if "0013009" in codes or not mine:
        return _cond("취업 상태", labels, c["employment_status"], UNKNOWN, note="대상 '기타' 포함 또는 고객 취업 상태 미확인")
    return _cond("취업 상태", labels, c["employment_status"], NOT_MET)


def check_school(p):
    codes = _codes(p["school_cd"])
    if not codes or "0049010" in codes:
        return None
    return _cond("학력·학적", _labels(codes, SCHOOL), None, UNKNOWN, note="고객 학력·재학 여부 미입력")


def check_special(p, c):
    codes = [x for x in _codes(p["special_cd"]) if x != "0014009"]
    if not codes or "0014010" in codes:
        return None
    labels = _labels(codes, SPECIAL)
    if codes == ["0014002"]:
        return _cond("특화 대상", labels, c["sex"], MET if c["sex"] == "여" else NOT_MET)
    if codes == ["0014001"]:
        result = MET if c["employer_size"] == "중소기업" else UNKNOWN
        return _cond("특화 대상", labels, c["employer_size"], result, note="중소기업 재직 여부 확인 필요")
    return _cond("특화 대상", labels, None, UNKNOWN, note="해당 집단 여부 미입력")


def check_marriage(p, c):
    status = MARRIAGE.get(p["marriage_status"] or "", "제한없음")
    if status == "제한없음":
        return None
    result = UNKNOWN if not c["marital_status"] else (MET if c["marital_status"] == status else NOT_MET)
    return _cond("혼인", status, c["marital_status"], result)


def check_groups(p, c) -> list[dict]:
    out = []
    for pattern, label, judge in _GROUP_RULES:
        if re.search(pattern, p["policy_name"] or ""):
            result = judge(c) if judge else UNKNOWN
            out.append(_cond("대상 집단(정책명)", label, None, result, basis="정책명 키워드",
                             note="정책명으로 추정한 대상. 공고 확인 필요" if result == UNKNOWN else "정책명 기준 추정 판정"))
    return out


def check_homeless(p, c):
    if "무주택" not in (p["search_text"] or "") + (p["income_text"] or ""):
        return None
    if c["home_owner"] is None:
        return _cond("무주택", "무주택", None, UNKNOWN)
    return _cond("무주택", "무주택", "무주택" if not c["home_owner"] else "주택 보유",
                 MET if not c["home_owner"] else NOT_MET)


def _income_for_policy(c) -> tuple[int | None, str, bool, bool]:
    """정책 '본인 소득' 비교에 쓸 (연소득, 지표, 근사 여부, 하한 여부). 겸업자의 근로소득은 전체 소득의 하한이다."""
    if ("근로", "총급여") in c["income"] and c["customer_type"] == "근로":
        return c["income"][("근로", "총급여")]["amount_won"], "총급여", False, False
    if ("근로", "총급여") in c["income"] and c["customer_type"] == "겸업":
        return c["income"][("근로", "총급여")]["amount_won"], "총급여(외주 소득 제외)", True, True
    if ("사업", "사업소득금액") in c["income"] and c["customer_type"] == "사업·프리랜서":
        return c["income"][("사업", "사업소득금액")]["amount_won"], "사업소득금액(종합소득금액 근사)", True, False
    return None, "", True, False


def _compare(name, limit, value, basis, approximate, policy_label, customer_label, lower_bound=False):
    if value is None:
        return _cond(name, policy_label, None, UNKNOWN, note="고객 소득 지표 없음")
    if lower_bound and value <= limit:
        return _cond(name, policy_label, customer_label, UNKNOWN, basis, "다른 소득을 더하면 기준을 넘을 수 있음", True)
    if approximate and abs(value - limit) <= limit * 0.1:
        return _cond(name, policy_label, customer_label, UNKNOWN, basis, "근사 지표가 기준선 ±10% 안", True)
    return _cond(name, policy_label, customer_label, MET if value <= limit else NOT_MET, basis, approximate=approximate)


def check_income(p, c, median: dict[int, int]) -> list[dict]:
    basis = p["income_basis"]
    if basis == "무관":
        return [_cond("소득", "무관", None, MET)]
    if basis == "미상" or p["condition_confidence"] == "none":
        return [_cond("소득", p["income_text"] or "공고 참조", None, UNKNOWN, note="소득 기준 수치를 원문에서 못 찾음. 공고 확인")]
    annual, metric, approx, lower = _income_for_policy(c)
    hh = c["household_size"]
    out = []
    text = p["income_text"] or ""
    if p["personal_median_pct_max"] or p["household_median_pct_max"]:
        if annual is not None and hh:
            pct = annual / 12 / median[1] * 100  # 본인 소득 기준 1인 중위소득 대비
            if p["personal_median_pct_max"]:
                lim = p["personal_median_pct_max"]
                out.append(_compare("본인 소득(중위소득 %)", lim, pct, f"{metric}/12 ÷ 1인 기준중위소득", True,
                                    f"{lim:g}% 이하", f"{pct:.0f}%", lower))
            if p["household_median_pct_max"]:
                lim = p["household_median_pct_max"]
                if re.search(r"원가구|부모", text) and c["parents_household"] in (None, "별도 거주"):
                    out.append(_cond("가구 소득(중위소득 %)", f"원가구 {lim:g}% 이하", None, UNKNOWN,
                                     note="원가구(부모) 소득 미입력"))
                else:
                    hh_pct = annual / 12 / median[hh] * 100
                    out.append(_compare("가구 소득(중위소득 %)", lim, hh_pct,
                                        f"{metric}/12 ÷ {hh}인 기준중위소득. 소득인정액(재산 환산) 미반영", True,
                                        f"{lim:g}% 이하", f"{hh_pct:.0f}%", lower))
        else:
            out.append(_cond("소득(중위소득 %)", text[:80], None, UNKNOWN, note="소득 또는 가구원 수 미입력"))
    if p["youth_annual_income_max_won"]:
        # 대상별 기준(청년/청년 외/신혼부부)이 있으면 청년 기준으로 본다
        lim = p["youth_annual_income_max_won"]
        out.append(_compare("본인 연소득(청년 기준)", lim, annual, metric, approx, f"{lim:,}원 이하",
                            f"{annual:,}원" if annual is not None else None, lower))
    elif p["annual_income_max_won"]:
        lim = p["annual_income_max_won"]
        out.append(_compare("본인 연소득", lim, annual, metric, approx, f"{lim:,}원 이하",
                            f"{annual:,}원" if annual is not None else None, lower))
    if p["monthly_income_max_won"] and annual is not None:
        lim = p["monthly_income_max_won"]
        out.append(_compare("월소득", lim, annual / 12, f"{metric}/12", True, f"{lim:,}원 이하", f"{annual / 12:,.0f}원", lower))
    if p["couple_annual_income_max_won"] and c["marital_status"] != "미혼":
        out.append(_cond("부부합산 연소득", f"{p['couple_annual_income_max_won']:,}원 이하", None, UNKNOWN,
                         note="배우자 소득 미입력"))
    if p["asset_max_won"]:
        lim = p["asset_max_won"]
        out.append(_compare("재산", lim, c["net_assets_won"], "순자산(자산-부채). 정책별 재산 산정과 다를 수 있음",
                            True, f"{lim:,}원 이하", f"{c['net_assets_won']:,}원"))
    return out or [_cond("소득", text[:80], None, UNKNOWN, note="소득 조건 해석 불가")]


# ── 정책 하나 판정 ───────────────────────────────────────────
def judge(p: dict, c: dict, median: dict[int, int]) -> dict:
    conds = [check_age(p, c), check_job(p, c), *check_income(p, c, median)]
    for extra in (check_school(p), check_special(p, c), check_marriage(p, c), check_homeless(p, c)):
        if extra:
            conds.append(extra)
    conds.extend(check_groups(p, c))
    results = {x["result"] for x in conds}
    verdict = "불충족" if NOT_MET in results else ("확인 필요" if UNKNOWN in results else "입력상 충족")
    return {
        "policy_id": p["policy_id"],
        "policy_name": p["policy_name"],
        "institution": p["institution"],
        "category": p["category_main"],
        "verdict": verdict,
        "conditions": conds,
        "benefit": {
            "type": p["benefit_type"], "summary": p["summary_line"],
            "monthly_won": p["monthly_amount_won"], "duration_months": p["duration_months"],
            "total_won": p["total_amount_won"], "range_won": [p["amount_min_won"], p["amount_max_won"]]
            if p["amount_max_won"] else None,
            "loan_limit_won": p["loan_limit_won"], "interest_support_pct": p["interest_support_pct"],
            "discount_pct": p["discount_pct"], "discount_won": p["discount_won"],
            "match_ratio": p["match_ratio"], "own_saving_monthly_won": p["own_saving_monthly_won"],
            "confidence": p["benefit_confidence"],
            "planning_note": "선정 전 지원금은 확정 수입이 아님. 별도 시나리오로 사용",
        },
        "apply": {"recruit_status": p["recruit_status"], "apply_end_date": p["apply_end_date"],
                  "url": p["application_url"] or p["ref_url1"]},
        "evidence": {"source": "온통청년 청년정책 API", "policy_id": p["policy_id"],
                     "income_text": p["income_text"] or None, "condition_confidence": p["condition_confidence"]},
        "dedup_key": p["dedup_key"],
    }


_FINANCIAL = {"매칭저축", "저축상품", "이자지원", "보증료지원", "대출", "월세·주거비", "할인·환급", "현금지원", "바우처·현물"}


def _rank(f):
    verdict = {"입력상 충족": 0, "확인 필요": 1}.get(f["verdict"], 2)
    unknowns = sum(x["result"] == UNKNOWN for x in f["conditions"])
    fin = 0 if f["benefit"]["type"] in _FINANCIAL else 1
    value = f["benefit"]["total_won"] or f["benefit"]["loan_limit_won"] or 0
    return (verdict, unknowns, fin, -value)


_QUESTION_BY_CONDITION = {
    "학력·학적": "현재 학력과 재학 여부를 알려주세요",
    "특화 대상": "중소기업 재직·장애·수급 등 해당하는 특화 대상이 있는지 알려주세요",
    "가구 소득(중위소득 %)": "원가구(부모) 소득·재산 또는 가구 소득인정액을 알려주세요",
    "부부합산 연소득": "배우자 소득을 알려주세요",
    "취업 상태": "현재 취업 상태를 확인해주세요",
}


# ── 전체 실행 ────────────────────────────────────────────────
def match(policy_conn: sqlite3.Connection, customer: dict) -> dict:
    policy_conn.row_factory = sqlite3.Row
    median = dict(policy_conn.execute("SELECT household_size, monthly_won FROM median_income"))
    rows = policy_conn.execute("""
        SELECT v.*, p.institution, p.job_cd, p.school_cd, p.special_cd, p.marriage_status, p.search_text,
               p.ref_url1, p.region AS region_codes, c.income_text
        FROM v_policy_finance v JOIN policies p USING (policy_id) JOIN policy_conditions c USING (policy_id)
        WHERE v.recruit_status IN ('open', 'always')""").fetchall()
    live = len(rows)
    in_region = [dict(r) for r in rows if customer["region_code"] and customer["region_code"] in (r["region_codes"] or "")]
    personal = [p for p in in_region
                if not _ADMIN_RE.search(p["policy_name"] or "")
                and not any(cat in (p["category_sub"] or "") for cat in _ADMIN_CATEGORY)]

    judged = [judge(p, customer, median) for p in personal]
    policy_judged = len(judged)
    policy_not_eligible = sum(1 for f in judged if f["verdict"] == "불충족")
    # 복지로 복지서비스. 온통청년에 같은 사업이 있으면 온통청년 판정을 쓴다
    known_names = [n for (n,) in policy_conn.execute("SELECT policy_name FROM policies")]
    known_names += [n for (n,) in policy_conn.execute("SELECT product_name FROM asset_products")]
    welfare, welfare_counts = match_welfare(policy_conn, customer, median, known_names)
    welfare_counts["not_eligible"] = sum(1 for f in welfare if f["verdict"] == "불충족")
    # 생애주기가 넓은 상황형 서비스는 본 결과와 지출 연결에 넣지 않고 따로 둔다(재무 상담에서 참고)
    situational = sorted((f for f in welfare if not f["youth_specific"] and f["verdict"] != "불충족"), key=_rank)
    judged += [f for f in welfare if f["youth_specific"]]
    excluded = Counter()
    for f in judged:
        if f["verdict"] == "불충족":
            for x in f["conditions"]:
                if x["result"] == NOT_MET:
                    excluded[x["condition"]] += 1
    kept = sorted((f for f in judged if f["verdict"] != "불충족"), key=_rank)
    # 같은 사업의 중복 공고는 가장 좋은 판정 하나만 남긴다
    seen, unique = set(), []
    for f in kept:
        if f["dedup_key"] in seen:
            continue
        seen.add(f["dedup_key"])
        unique.append(f)
    # 자금 배분(4번)에 쓰는 금융성 혜택과 그 밖의 서비스를 나눈다
    findings = [f for f in unique if f["benefit"]["type"] in _FINANCIAL]
    other = [f for f in unique if f["benefit"]["type"] not in _FINANCIAL]

    questions = Counter()
    for f in unique:
        for x in f["conditions"]:
            # 취업 상태를 이미 아는데 정책 쪽 대상이 '기타'라서 미확인이면 고객에게 물어도 풀리지 않는다
            if x["result"] == UNKNOWN and x["condition"] in _QUESTION_BY_CONDITION \
                    and not (x["condition"] == "취업 상태" and x["customer_value"]):
                questions[_QUESTION_BY_CONDITION[x["condition"]]] += 1
    as_of_db = policy_conn.execute("SELECT MAX(as_of) FROM policy_conditions").fetchone()[0]
    counts = Counter(f["verdict"] for f in findings)
    other_counts = Counter(f["verdict"] for f in other)
    return {
        "module": "policy",
        "case_id": customer["case_id"],
        "profile_version": customer["profile_version"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "completed" if findings else "partial",
        "search_scope": {
            "source": "온통청년 청년정책 + 복지로 복지서비스(보강 DB)", "policy_db_as_of": as_of_db,
            "filters": ["모집 중·상시", f"지역 코드 {customer['region_code']} 포함", "연령·취업·소득·대상 조건 판정",
                        "복지로: 청년 생애주기, 중앙부처 + 고객 시도·시군구 지자체, 온통청년과 같은 사업 제외"],
            "counts": {"live_policies": live, "in_region": len(in_region),
                       "excluded_admin": len(in_region) - len(personal), "judged": policy_judged,
                       "not_eligible": policy_not_eligible, "welfare": welfare_counts,
                       "kept_after_dedup": len(unique), "financial": len(findings), "other": len(other),
                       "from_welfare": sum(f["policy_id"].startswith("WLF") for f in unique)},
            "not_covered": ["금감원 금융상품(별도 모듈)", "온통청년·복지로 미등록 정책", "세제 혜택(별도 세금 계산)"],
        },
        "customer_snapshot": {k: customer[k] for k in
                              ("as_of", "age", "region_code", "household_size", "marital_status", "home_owner",
                               "employment_status", "customer_type", "net_assets_won")},
        "summary": {"금융성 혜택": {"입력상 충족": counts.get("입력상 충족", 0), "확인 필요": counts.get("확인 필요", 0)},
                    "기타 서비스": {"입력상 충족": other_counts.get("입력상 충족", 0),
                               "확인 필요": other_counts.get("확인 필요", 0)},
                    "불충족 사유": dict(excluded.most_common())},
        "findings": findings,
        "other_findings": other,
        "situational_findings": situational,
        "missing_fields": sorted({x["condition"] for f in unique for x in f["conditions"] if x["result"] == UNKNOWN}),
        "questions": [q for q, _ in questions.most_common()],
        "disclaimer": "입력상 충족은 기관의 최종 선정과 다릅니다. 지원금은 선정 전까지 확정 수입으로 계획에 넣지 않습니다.",
    }


def save(customer_conn: sqlite3.Connection, result: dict) -> None:
    customer_conn.execute("INSERT OR REPLACE INTO module_results VALUES (?,?,?,?,?,?)",
                          (result["case_id"], result["profile_version"], "policy", result["status"],
                           json.dumps(result, ensure_ascii=False), result["generated_at"]))
    customer_conn.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("--version")
    parser.add_argument("--db", default=Path("data/customer_profiles.db"), type=Path)
    parser.add_argument("--policy-db", default=Path("data/youth_policy_enriched.db"), type=Path)
    parser.add_argument("--out", default=Path("output"), type=Path)
    args = parser.parse_args()

    customer_conn = sqlite3.connect(args.db)
    customer = load_customer(customer_conn, args.case_id, args.version)
    with sqlite3.connect(args.policy_db) as policy_conn:
        result = match(policy_conn, customer)
    save(customer_conn, result)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"policy_{result['case_id']}_{result['profile_version']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"file": str(path), **result["search_scope"]["counts"], **{k: v for k, v in result["summary"].items()}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
