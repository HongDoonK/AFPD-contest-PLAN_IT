from policy_enrich.benefits import build_benefit


def _b(name, content):
    return build_benefit({"policy_id": "P1", "policy_name": name, "support_content": content})


def test_matching_saving_with_explicit_amounts():
    b = _b("청년내일저축계좌 지원", "○ 근로소득장려금 지원\n· 매월 10만원 저축 시 정부가 월 30만원 지원\n3년간")
    assert b.benefit_type == "매칭저축"
    assert b.own_saving_monthly_won == 100_000
    assert b.match_monthly_won == 300_000
    assert b.match_ratio == 3.0
    assert b.duration_months == 36
    assert b.total_amount_won == 10_800_000
    assert b.parse_confidence == "high"


def test_matching_saving_same_amount():
    b = _b("청년미래적금 운영", "관내 기업에 근로 중인 청년이 월 15만원의 적금 납입 시에 시도 동일금액을 적립하여 만기(3년)에 일시 지급")
    assert b.benefit_type == "매칭저축"
    assert b.match_ratio == 1.0
    assert b.own_saving_monthly_won == 150_000
    assert b.match_monthly_won == 150_000


def test_ratio_notation():
    b = _b("청년내일저축계좌", "본인저축액(10만원)에 대해 근로소득장려금 1:3 매칭")
    assert b.match_ratio == 3.0
    assert b.own_saving_monthly_won == 100_000


def test_monthly_rent():
    b = _b("청년 월세 지원사업", "월 최대 20만원, 최대 12개월 지원")
    assert b.benefit_type == "월세·주거비"
    assert b.monthly_amount_won == 200_000
    assert b.duration_months == 12
    assert b.total_amount_won == 2_400_000


def test_interest_support():
    b = _b("청년 전세자금 대출이자 지원", "대출한도 최대 1억원, 연 2% 이자 지원")
    assert b.benefit_type == "이자지원"
    assert b.loan_limit_won == 100_000_000
    assert b.interest_support_pct == 2.0
    assert b.total_amount_won is None  # 대출 한도 1억원은 지원 총액이 아니다


def test_mentoring_one_to_one_is_not_matching_saving():
    b = _b("청년 진로 멘토링", "전문가 1:1 매칭 멘토링 제공")
    assert b.benefit_type != "매칭저축"


def test_unknown_amount():
    b = _b("청년 네트워크", "청년 참여 활동 운영")
    assert b.parse_confidence == "none"
    assert "공고 확인" in b.summary_line


def test_lump_sum_single_amount():
    b = _b("인구감소지역 결혼지원금", "도내 인구감소지역 신혼부부당 결혼지원금 100만원")
    assert b.benefit_type == "현금지원"
    assert b.total_amount_won == 1_000_000


def test_per_time_times_count():
    b = _b("경기도 청년 면접수당", "취업 면접에 참여한 18~39세 청년에게 면접수당 1회 5만원 지역화폐 지원 (최대 3회)")
    assert b.total_amount_won == 150_000


def test_amount_times_months():
    b = _b("청년도전지원사업", "참여수당 150만원 지급(50만원*3개월)")
    assert b.monthly_amount_won == 500_000
    assert b.duration_months == 3
    assert b.total_amount_won == 1_500_000


def test_tiered_amounts_give_range_not_total():
    b = _b("출산장려금 지원", "첫째 100만원, 둘째 200만원, 셋째 300만원, 넷째 이상 500만원")
    assert b.total_amount_won is None
    assert (b.amount_min_won, b.amount_max_won) == (1_000_000, 5_000_000)
    assert b.parse_confidence == "partial"


def test_condition_amounts_are_not_benefits():
    b = _b("전세보증금 반환보증 보증료 지원",
           "임차보증금 3억원 이하 가입 청년, 연소득 5천만원 이하 / 지원금액 : 반환보증보증료 가입비(40만원 이내 실비)")
    assert b.amount_max_won == 400_000


def test_loose_loan_limit():
    b = _b("청년 전월세 대출보증", "보증한도*: (전세) 최대 2억원, (월세) 최대 1,200만원")
    assert b.benefit_type == "대출"
    assert b.loan_limit_won == 200_000_000


def test_text_only_financial_keyword_needs_amount():
    assert _b("청년 자산관리 주치의제 운영", "저축·적금·주식 등 목표에 따른 투자법 상담").benefit_type == "교육·서비스"
    assert _b("청년 도서구입비 지원", "도서대출회원증 소지 청년 도서구입비 50%지원(1인 최대 10만원)").benefit_type == "현금지원"


def test_monthly_with_count_of_payments():
    b = _b("부산 자립준비청년 자립수당 지원", "지원내용: 1인 월 50만원(60회)")
    assert b.duration_months == 60
    assert b.total_amount_won == 30_000_000


def test_age_in_months_is_not_duration():
    b = _b("부모급여(영아수당) 지원", "(대상) 0~23개월 아동의 보호자 / (주요내용) (0세)100만원/월, (1세)50만원/월")
    assert b.duration_months is None
    assert b.monthly_amount_won == 1_000_000


def test_course_max_beats_first_course():
    b = _b("서구 청년도전지원사업", "수당(최대 350만원) 제공 / - 단기(5주, 50만원×1개월) / - 장기(25주, 50만원×5개월)")
    assert b.total_amount_won == 3_500_000


def test_deposit_cap_is_loan_limit_not_total():
    b = _b("신혼부부 임차보증금 이자지원", "부부합산 연소득 1억원 이하 / 전세 임차보증금(최대 2억원)의 이자지원(최대 5.5%)")
    assert b.loan_limit_won == 200_000_000
    assert b.interest_support_pct == 5.5
    assert b.total_amount_won is None


def test_subscription_account_is_saving_product():
    assert _b("청년주택드림청약통장", "이자율: 최대 4.5% / 납입한도: 월 100만원 / 청년주택드림 대출 연계").benefit_type == "저축상품"


def test_monthly_rent_cap_with_years():
    b = _b("청년창업 특화거리 점포 임차료 지원사업", "월 임차료 30만원 한도 내 지원(최대 2년)")
    assert b.monthly_amount_won == 300_000
    assert b.duration_months == 24
    assert b.total_amount_won == 7_200_000


def test_monthly_max_is_not_total():
    b = _b("청년 창업농 주거지원 사업", "월세 및 전세자금 대출이자 지원(월 최대 50만원) / 융자 추천")
    assert b.monthly_amount_won == 500_000
    assert b.total_amount_won is None


def test_transit_refund_uses_youth_rate():
    b = _b("K-패스(K패스)", "월 15회 이상 대중교통 이용 시 지출액의 일정비율 환급 / - 일반층 : 20% / - 청년층 : 30%")
    assert b.benefit_type == "할인·환급"
    assert b.discount_pct == 30


def test_fixed_discount_and_fee_share():
    assert _b("2026 기후동행카드(청년할인 서비스)", "일반권 권종 대비 7천원 할인 제공").discount_won == 7_000
    b = _b("청년 국가기술자격 응시료 지원 사업", "국가기술자격 시험에 응시하는 경우 응시료의 50%를 선 지원")
    assert b.benefit_type == "현금지원" and b.discount_pct == 50


def test_culture_pass_is_discount_type():
    assert _b("경기 컬처패스", "1인당 지원 한도(2.5만원 → 6만원 내) 확대").benefit_type == "할인·환급"
