"""직접 확인해 관리하는 기준 데이터.

API 원문만으로는 부족하거나 틀린 값을 사람이 출처와 확인일을 붙여 관리한다.
값을 바꿀 때는 source_url과 verified_at도 함께 고친다.
"""
from __future__ import annotations

# 보건복지부 고시 2026년 기준 중위소득(월, 원). 원 저장소 frontend/src/medianIncome.js와 같은 값.
MEDIAN_INCOME_YEAR = 2026
MEDIAN_INCOME_MONTHLY = {1: 2_564_238, 2: 4_199_292, 3: 5_359_036, 4: 6_494_738,
                         5: 7_556_719, 6: 8_555_952, 7: 9_515_150}
MEDIAN_INCOME_EXTRA_PER_PERSON_OVER_7 = 959_198


def median_income(household_size: int) -> int:
    size = max(1, int(household_size))
    if size <= 7:
        return MEDIAN_INCOME_MONTHLY[size]
    return MEDIAN_INCOME_MONTHLY[7] + MEDIAN_INCOME_EXTRA_PER_PERSON_OVER_7 * (size - 7)


# 여유 자금 계획에서 가장 먼저 비교해야 하는 중앙정부 자산형성 상품.
# 수치는 원문에 나온 것만 적고, 원문에서 확인하지 못한 항목은 None + notes에 남긴다.
ASSET_PRODUCTS = [
    {
        "product_id": "youth_future_savings",
        "product_name": "청년미래적금",
        "operator": "금융위원회·서민금융진흥원",
        "status": "모집회차제",  # 1차 2026-06-22~07-03, 이후 회차는 공지 확인
        "age_min": 19, "age_max": 34,
        "age_note": "병역 이행 기간(최대 6년)은 연령 계산에서 제외",
        "term_months": 36,
        "monthly_limit_won": 500_000,
        "gov_match_pct_general": 6.0,
        "gov_match_pct_preferred": 12.0,
        "tax_free": 1,
        "income_condition": "직전연도 소득 확인 필요. 우대형: 요건 충족 중소기업 재직자·중소기업 신규 취업자·소상공인. "
                            "개인·가구 소득 상한 금액은 보도자료 본문에 없음(취급은행 공지 확인 필요)",
        "apply_window": "1차 2026-06-22 ~ 2026-07-03",
        "channels": "기업, 농협, 신한, 우리, 하나, 국민, iM, 부산, 경남, 광주, 전북, 수협, 카카오, 우정사업본부",
        "source_url": "https://www.fsc.go.kr/no010101/87158",
        "verified_at": "2026-11-04",
        "notes": "기여금 = 납입액 x 매칭률. 월 50만원 36개월 기준 일반형 108만원, 우대형 216만원.",
    },
    {
        "product_id": "youth_leap_account",
        "product_name": "청년도약계좌",
        "operator": "금융위원회·서민금융진흥원",
        "status": "신규가입중단",
        "age_min": 19, "age_max": 34,
        "age_note": None,
        "term_months": 60,
        "monthly_limit_won": 700_000,
        "gov_match_pct_general": None,
        "gov_match_pct_preferred": 6.0,
        "tax_free": 1,
        "income_condition": "소득구간·납입액에 따라 최대 6% 정부기여금",
        "apply_window": None,
        "channels": None,
        "source_url": "온통청년 DB 원문(청년 자산형성 지원(청년도약계좌))",
        "verified_at": "2026-11-04",
        "notes": "'25.12.31 비과세 일몰로 신규 가입 중단. 기존 가입자의 유지·전환 판단용으로만 쓴다. "
                 "월 70만원·60개월은 기존 상품 안내 기준이며 이번에 원문으로 다시 확인하지 않았다.",
    },
    {
        "product_id": "youth_tomorrow_savings",
        "product_name": "청년내일저축계좌",
        "operator": "보건복지부(지자체 접수)",
        "status": "연1회모집",
        "age_min": 15, "age_max": 39,
        "age_note": "차상위 초과 구간은 19~34세",
        "term_months": 36,
        "monthly_limit_won": 500_000,
        "gov_match_pct_general": None,
        "gov_match_pct_preferred": None,
        "tax_free": 0,
        "income_condition": "근로·사업소득 월 10만원 이상. 가구 소득인정액 중위소득 50% 이하: 월 10만원 저축 시 30만원 매칭(1:3), "
                            "50% 초과~100% 이하: 10만원 매칭(1:1)",
        "apply_window": "2026-05-04 ~ 2026-05-20 (온통청년 DB 기준)",
        "channels": "주소지 읍면동 행정복지센터·복지로",
        "source_url": "온통청년 DB 원문(청년내일저축계좌 지원)",
        "verified_at": "2026-11-04",
        "notes": "정액 매칭이라 매칭률(%) 대신 matching_rule 텍스트로 계산한다. 본인저축 상한은 지자체 공고 기준 50만원.",
    },
]
