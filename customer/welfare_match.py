"""복지로 복지서비스(welfare_services) × 고객 입력 → 정책 판정과 같은 모양의 결과.

온통청년 정책과 달리 복지로는 나이·소득·취업 조건을 칸으로 주지 않는다. 그래서
- 지역: 중앙부처는 전국, 지자체는 시도가 같고 시군구가 같거나 비어 있는(시도 전역) 것만 본다.
- 나이: 지원대상·선정기준 문장의 'N~M세', 'N세 이하'를 읽고, 없으면 생애주기의 '청년'으로 본다.
- 소득: 정책 DB와 같은 문장 해석기(policy_enrich.conditions)로 중위소득·연소득 상한을 읽는다.
- 가구 상황(장애인·보훈·한부모 등)은 고객 입력에 없으므로 미확인으로 둔다.
- 온통청년에 같은 사업이 있으면 그쪽 판정(조건이 칸으로 있어 더 정확)을 쓰고 복지로 쪽은 뺀다.
판정은 '입력상 충족'이지 기관의 선정이 아니다.
"""
from __future__ import annotations

import json
import re
import sqlite3

from policy_enrich.benefits import build_benefit
from policy_enrich.conditions import _has_income_signal, build_condition, dedup_key

from .region_map import REGION_CODE_MAP

# 행정표준코드 앞 두 자리 → 복지로 시도 이름(2026-11 복지로 응답 기준. 광주·전남은 통합특별시로 나온다)
SIDO_BY_PREFIX = {
    "11": "서울특별시", "26": "부산광역시", "27": "대구광역시", "28": "인천광역시", "29": "전남광주통합특별시",
    "30": "대전광역시", "31": "울산광역시", "36": "세종특별자치시", "41": "경기도", "42": "강원특별자치도",
    "51": "강원특별자치도", "43": "충청북도", "44": "충청남도", "45": "전북특별자치도", "52": "전북특별자치도",
    "46": "전남광주통합특별시", "47": "경상북도", "48": "경상남도", "50": "제주특별자치도",
}
# 일반구가 있는 시. 코드 앞 네 자리가 같으면 같은 시다(복지로는 '성남시'처럼 시 단위로 적는다)
CITY_BY_PREFIX = {
    "4111": "수원시", "4113": "성남시", "4117": "안양시", "4119": "부천시", "4127": "안산시", "4128": "고양시",
    "4146": "용인시", "4311": "청주시", "4413": "천안시", "4511": "전주시", "5211": "전주시", "4711": "포항시",
    "4812": "창원시",
}
_SPECIAL_GROUPS = ("장애인", "보훈대상자", "한부모·조손", "다문화·탈북민", "다자녀")
_NOT_EMPLOYED_RE = re.compile(r"미취업|구직\s*(?:중|자|청년)|실업자|취업\s*준비")
_HOMELESS_RE = re.compile(r"무주택")
_AGE_RANGE_RE = re.compile(r"(?:만\s*)?(\d{1,2})\s*세?\s*(?:이상\s*)?[~∼\-]\s*(?:만\s*)?(\d{1,2})\s*세"
                           r"|(?:만\s*)?(\d{1,2})\s*세\s*이상\s*(?:만\s*)?(\d{1,2})\s*세\s*(이하|미만)")
_AGE_UPPER_RE = re.compile(r"(?:만\s*)?(\d{1,2})\s*세\s*(이하|미만|까지)")
_REGION_PREFIX_RE = re.compile(r"^\s*(?:[\[(（]?[가-힣]{1,8}(?:특별시|광역시|특별자치시|특별자치도|도|시|군|구)[\])）]?\s+){1,2}")
_ADMIN_RE = re.compile(r"연찬회|위원회|협의체|홍보|실태조사|교육청|종사자")


_YOUTH_STAGES = {"청년", "청소년"}


def is_youth_specific(row: dict) -> bool:
    """청년 특화 서비스인지. 이름에 '청년'이 있거나 생애주기가 청년·청소년뿐이면 특화로 본다.
    생애주기가 넓은 서비스(의사상자 지원, 체불임금 대지급 등)는 특정 상황에 놓인 사람 대상이라 따로 둔다."""
    if "청년" in (row["service_name"] or ""):
        return True
    stages = {x.strip() for x in (row["life_cycle"] or "").split(",") if x.strip()}
    return bool(stages) and stages <= _YOUTH_STAGES


def customer_region(code: str | None) -> tuple[str | None, set[str]]:
    """행정표준코드 → (복지로 시도 이름, 같은 지역으로 볼 시군구 이름들)."""
    if not code:
        return None, set()
    code = str(code)
    names = set()
    for districts in REGION_CODE_MAP.values():
        for name, value in districts.items():
            if value == code.ljust(5, "0")[:5]:
                names.add(name)
    if code[:4] in CITY_BY_PREFIX:
        names.add(CITY_BY_PREFIX[code[:4]])
    return SIDO_BY_PREFIX.get(code[:2]), names


def core_key(name: str | None) -> str:
    """'성남시 청년기본소득'과 '청년기본소득'을 같은 사업으로 보려고 앞의 지역명을 뗀 뒤 dedup_key를 만든다."""
    return dedup_key(_REGION_PREFIX_RE.sub("", name or ""))


def is_duplicate(key: str, known: set[str]) -> bool:
    """같은 사업인지. 지역명을 뗀 이름이 같거나, 5글자 이상인 한쪽 이름이 다른 쪽에 들어 있으면 같다고 본다
    ('청년내일저축계좌'와 '청년내일저축계좌 지원사업')."""
    if key in known:
        return True
    return len(key) >= 5 and any(len(k) >= 5 and (k in key or key in k) for k in known)


def parse_ages(text: str) -> list[tuple[int, int]]:
    """문장 속 나이 범위들. 상한만 있으면 (0, 상한). 미만은 하나 뺀다."""
    out = []
    for m in _AGE_RANGE_RE.finditer(text):
        if m.group(1):
            lo, hi = int(m.group(1)), int(m.group(2))
        else:
            lo, hi = int(m.group(3)), int(m.group(4)) - (m.group(5) == "미만")
        if lo <= hi:
            out.append((lo, hi))
    if not out:
        for m in _AGE_UPPER_RE.finditer(text):
            out.append((0, int(m.group(1)) - (m.group(2) == "미만")))
    return out


def _cond(name, policy_value, customer_value, result, basis="", note="", approximate=False):
    return {"condition": name, "policy_value": policy_value, "customer_value": customer_value,
            "result": result, "basis": basis, "note": note, "approximate": approximate}


def check_age(row: dict, c: dict, text: str) -> dict:
    from .policy_match import MET, NOT_MET, UNKNOWN
    # 자녀 나이(0~7세 등)는 고객 나이 조건이 아니다. 성인 범위만 본다
    ranges = [r for r in parse_ages(text) if r[1] >= 18]
    if ranges:
        label = ", ".join(f"{lo or ''}~{hi}세" for lo, hi in ranges)
        if c["age"] is None:
            return _cond("연령", label, None, UNKNOWN, note="생년월일 필요")
        ok = any(lo <= c["age"] <= hi for lo, hi in ranges)
        return _cond("연령", label, f"만 {c['age']}세", MET if ok else NOT_MET, basis="복지로 지원대상 문장")
    if "청년" in (row["life_cycle"] or "") or "청년" in (row["service_name"] or ""):
        return _cond("연령", "생애주기: 청년", f"만 {c['age']}세" if c["age"] is not None else None, MET,
                     basis="복지로 생애주기", note="정확한 나이 기준은 공고 확인", approximate=True)
    return _cond("연령", row["life_cycle"] or "미상", c["age"], UNKNOWN, note="청년 대상인지 공고 확인")


def judge_welfare(row: dict, c: dict, median: dict[int, int]) -> dict:
    from .policy_match import MET, NOT_MET, UNKNOWN, check_groups, check_income

    text = "\n".join(t for t in (row["target_detail"], row["selection_criteria"]) if t)
    sido, names = customer_region(c["region_code"])
    where = "전국(중앙부처)" if row["scope"] == "central" else \
        f"{row['region_sido']} {row['region_sigungu'] if row['region_sigungu'] not in (None, '-') else '전역'}"
    conds = [_cond("지역", where, f"{sido or ''} {'·'.join(sorted(names))}".strip(), MET, basis="복지로 시도·시군구"),
             check_age(row, c, text)]

    condition = build_condition({"policy_id": row["service_id"], "income_type": None, "income_etc": text,
                                 "support_content": row["support_content"], "policy_name": row["service_name"]}, "")
    p = {**condition.as_row(), "condition_confidence": condition.parse_confidence}
    if condition.income_basis == "미상" and not _has_income_signal(text) and "저소득" not in (row["target_group"] or ""):
        # 온통청년은 '소득 무관'을 칸으로 주지만 복지로는 없다. 원문과 가구 상황에 소득 단서가 없으면 무관으로 본다
        conds.append(_cond("소득", "원문에 소득 기준 없음", None, MET, basis="복지로 지원대상·선정기준 문장"))
    else:
        conds += check_income(p, c, median)

    groups = [g for g in _SPECIAL_GROUPS if g in (row["target_group"] or "")]
    if groups:
        conds.append(_cond("특화 대상", ", ".join(groups), None, UNKNOWN, basis="복지로 가구 상황",
                           note="해당 집단 여부 미입력"))
    if _NOT_EMPLOYED_RE.search(text) and c["employment_status"] in ("재직", "프리랜서", "자영업"):
        conds.append(_cond("취업 상태", "미취업·구직 중", c["employment_status"], UNKNOWN,
                           note="원문이 미취업자 대상으로 보임. 공고 확인"))
    if _HOMELESS_RE.search(text):
        owner = c["home_owner"]
        conds.append(_cond("무주택", "무주택", None if owner is None else ("주택 보유" if owner else "무주택"),
                           UNKNOWN if owner is None else (NOT_MET if owner else MET)))
    conds += check_groups({"policy_name": row["service_name"]}, c)

    results = {x["result"] for x in conds}
    verdict = "불충족" if NOT_MET in results else ("확인 필요" if UNKNOWN in results else "입력상 충족")
    benefit = build_benefit({"policy_id": row["service_id"], "policy_name": row["service_name"],
                             "support_content": row["support_content"] or row["summary"]}).as_row()
    raw = json.loads(row["raw_json"] or "{}")
    end = (raw.get("detail") or {}).get("enfcEndYmd")
    return {
        "policy_id": row["service_id"],
        "policy_name": row["service_name"],
        "institution": row["agency"],
        "category": row["interest_theme"],
        "verdict": verdict,
        "conditions": conds,
        "benefit": {
            "type": benefit["benefit_type"], "summary": benefit["summary_line"],
            "monthly_won": benefit["monthly_amount_won"], "duration_months": benefit["duration_months"],
            "total_won": benefit["total_amount_won"],
            "range_won": [benefit["amount_min_won"], benefit["amount_max_won"]] if benefit["amount_max_won"] else None,
            "loan_limit_won": benefit["loan_limit_won"], "interest_support_pct": benefit["interest_support_pct"],
            "discount_pct": benefit["discount_pct"], "discount_won": benefit["discount_won"],
            "match_ratio": benefit["match_ratio"], "own_saving_monthly_won": benefit["own_saving_monthly_won"],
            "confidence": benefit["parse_confidence"],
            "planning_note": "선정 전 지원금은 확정 수입이 아님. 별도 시나리오로 사용",
        },
        "apply": {"recruit_status": "always" if not end or end >= "99990000" else "open",
                  "apply_end_date": None if not end or end >= "99990000" else end, "url": row["service_url"]},
        "evidence": {"source": "복지로 " + ("중앙부처" if row["scope"] == "central" else "지자체") + " 복지서비스",
                     "policy_id": row["service_id"], "income_text": condition.income_text or None,
                     "condition_confidence": condition.parse_confidence, "support_cycle": row["support_cycle"],
                     "provision_method": row["provision_method"], "detail_collected": bool(row["detail_collected_at"])},
        "dedup_key": "welfare:" + core_key(row["service_name"]),
        "youth_specific": is_youth_specific(row),
    }


def match_welfare(conn: sqlite3.Connection, customer: dict, median: dict[int, int],
                  policy_names: list[str]) -> tuple[list[dict], dict]:
    """청년 대상 복지서비스 중 고객 지역에 해당하는 것을 판정한다. 테이블이 없으면 빈 결과.
    policy_names는 온통청년 전체 정책명과 자산형성 상품명이다. 온통청년에서 모집 마감이나 지역 때문에 빠진
    사업을 복지로 쪽에서 되살리지 않으려고, 판정 대상만이 아니라 전체 이름과 견준다."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='welfare_services'").fetchone():
        return [], {"skipped": "welfare_services 테이블 없음"}
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM welfare_services WHERE is_youth=1 OR service_name LIKE '%청년%'")]
    sido, names = customer_region(customer["region_code"])
    local = [r for r in rows if r["scope"] == "local" and r["region_sido"] == sido
             and (r["region_sigungu"] in (None, "-") or r["region_sigungu"] in names)]
    candidates = [r for r in rows if r["scope"] == "central"] + local
    today = (customer.get("as_of") or "").replace("-", "")
    open_rows = [r for r in candidates
                 if not (today and (json.loads(r["raw_json"] or "{}").get("detail") or {}).get("enfcEndYmd", "99999999") < today)]
    personal = [r for r in open_rows if not _ADMIN_RE.search(r["service_name"] or "")]
    known = {core_key(n) for n in policy_names}
    fresh = [r for r in personal if not is_duplicate(core_key(r["service_name"]), known)]
    judged = [judge_welfare(r, customer, median) for r in fresh]
    counts = {"youth_services": len(rows), "central": sum(r["scope"] == "central" for r in rows),
              "local_in_region": len(local), "closed": len(candidates) - len(open_rows),
              "excluded_admin": len(open_rows) - len(personal),
              "same_as_youth_policy": len(personal) - len(fresh), "judged": len(judged),
              "missing_detail": sum(not r["detail_collected_at"] for r in fresh),
              "youth_specific": sum(f["youth_specific"] for f in judged),
              "situational": sum(not f["youth_specific"] for f in judged)}
    return judged, counts
