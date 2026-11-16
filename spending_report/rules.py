"""소비 분류·조정 규칙과 보고서 고정 표.

reduce_pct와 floor_won은 '이만큼 줄이면 어떻게 되는가'를 보여 주는 임시값이다. 통계나 AFPK 기준에서 온 값이 아니므로
재무팀이 확정하기 전까지 보고서에 임시 가정으로 표시한다(.scratch/spending-report/issues/07).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RULES_VERSION = "2026-11-10-draft"

# FF 7번 표의 지출 묶음. 고객 입력의 보험은 저축 칸, 세금·사회보험은 고정지출 칸에 인쇄돼 있다
FF_BLOCK = {"저축": "저축", "보험": "저축", "세금·사회보험": "고정지출", "고정지출": "고정지출", "변동지출": "변동지출"}
FF_BLOCKS = ("저축", "고정지출", "변동지출")
INCOME_LABELS = {"세후 입금": "근로·사업소득(세후 수령액)", "매출(세전 월평균)": "사업매출(세전 월평균)",
                 "성과금": "성과금(인센티브)", "기타소득": "기타소득"}

# 이 보고서에서 금액을 바꾸지 않는 구역과 그 이유
SECTION_KEEP = {
    "저축": "저축 배분은 자금 배분 단계에서 정함",
    "보험": "보험 분석 결과가 나온 뒤 판단",
    "세금·사회보험": "납부 의무이거나 미래 납세를 위한 준비금",
}
MIXED_KEEP = "업무비가 섞여 있어 줄이면 세금이 달라질 수 있음. 세금 재계산 후 판단"
MIXED_QUESTION = "'{item}'의 생활비와 업무비 금액을 나눠 주세요"
RESERVE_LABEL = "절감 재원(자금 배분 대기)"

# 보고서 8번에 보여 줄 미입력 항목 이름
FIELD_LABELS = {"military_service": "병역 이행 기간", "employer_size": "재직 회사 규모",
                "national_pension_expected": "국민연금 예상액", "nationality": "국적·거주자 여부",
                "business_type": "사업자 유형", "vat_included": "매출의 부가세 포함 여부",
                "is_householder": "세대주 여부", "parents_household": "원가구 정보"}

# 카드값처럼 여러 지출이 묶인 행의 조건부 절감률(임시값). 명세서를 나누기 전에는 표에 넣지 않는다
LUMP_REVIEW_PCT = 0.10
BLANK_QUESTION = "금액이 비어 있는 항목의 월 금액을 알려주세요: {items}"
BLANK_INCOME_QUESTION = "수입 칸이 비어 있습니다. 월평균 금액을 알려주세요: {items}"
CARD_QUESTION = "카드 명세서 최근 3개월분을 주시면 카드값을 항목별로 나눠 다시 계산합니다"
ESTIMATED_KEEP = "금액이 추정값이라 거래내역을 확인한 뒤 판단"
BLANK_KEEP = "금액이 비어 있어 판단하지 못함"

# 유동성 지표에 넣는 자산(바로 또는 해지해서 쓸 수 있는 현금성 자산). 용도가 정해진 돈은 제외한다
LIQUID_KINDS = {"입출금", "파킹", "정기예금", "적금"}


@dataclass(frozen=True)
class SpendRule:
    pattern: str
    necessity: str  # 필수 / 일부조정 / 선택 / 미확인
    reduce_pct: float = 0.0
    floor_won: int = 0  # 이 금액 이하면 조정을 제안하지 않음
    action: str = ""
    keep_reason: str = ""
    question: str = ""
    when: str = "이번 달부터"   # 언제 시작하는가
    tradeoff: str = ""          # 바꿀 때의 불편이나 전환 비용


# 위에서부터 먼저 맞는 규칙을 쓴다
SPEND_RULES = (
    SpendRule(r"임대료|월세", "필수", keep_reason="주거 계약에 묶인 지출"),
    SpendRule(r"대출", "필수", keep_reason="계약상 상환액. 대환 검토는 자금 배분 단계"),
    SpendRule(r"세금", "필수", keep_reason="납부 의무"),
    SpendRule(r"용돈", "미확인", keep_reason="가족 지원은 고객이 판단할 지출",
              question="부모님 용돈을 조정 대상에서 제외할지 알려주세요"),
    SpendRule(r"공과금|관리", "필수", keep_reason="주거 유지 비용"),
    SpendRule(r"구독", "선택", 0.20, action="쓰지 않는 구독을 해지해 보세요.", when="이번 달 안에", tradeoff="연간 결제는 환불 조건을 먼저 확인",
              question="이용 중인 구독 목록과 최근 사용 여부를 알려주세요"),
    SpendRule(r"택시", "일부조정", 0.50, action="택시 대신 대중교통을 타는 날을 늘려 보세요.", tradeoff="심야 귀가와 야간 근무는 예외로 둠"),
    SpendRule(r"대중교통|교통", "필수", keep_reason="이동에 필요한 비용"),
    SpendRule(r"통신", "일부조정", 0.15, floor_won=60_000, action="요금제와 결합 할인을 점검해 보세요.", when="이번 달 안에", tradeoff="약정 위약금과 결합 해지 조건을 확인",
              keep_reason="조정 제안 기준 금액 이하",
              question="통신비에 단말기 할부금이 포함되는지 알려주세요"),
    SpendRule(r"직장\s*식사", "필수", keep_reason="근무 중 식사"),
    SpendRule(r"식재료|생필품", "필수", keep_reason="기본 생활비"),
    SpendRule(r"식비", "일부조정", 0.15, action="외식과 배달 횟수를 미리 정해 보세요.", when="이번 주부터", tradeoff="바쁜 주에는 준비 시간이 더 듦",
              question="식비 중 외식·배달 비중을 카드 내역으로 확인해 주세요"),
    SpendRule(r"배달|외식", "일부조정", 0.25, action="외식과 배달 횟수를 미리 정해 보세요.", when="이번 주부터", tradeoff="바쁜 주에는 준비 시간이 더 듦"),
    SpendRule(r"쇼핑", "선택", 0.20, action="구매 전에 며칠 기다리는 규칙을 정해 보세요.", when="이번 주부터", tradeoff="급하게 필요한 물건은 예외로 둠"),
    SpendRule(r"의료|약제", "필수", keep_reason="건강 유지 비용"),
    SpendRule(r"자기개발|교육", "미확인", keep_reason="직무 역량과 연결된 지출. 직업만으로 줄이지 않음"),
    SpendRule(r"문화", "선택", 0.10, action="문화생활비의 월 한도를 정해 보세요.", tradeoff="만족도가 큰 활동을 먼저 남기고 고객이 정함"),
    SpendRule(r"경조사", "미확인", keep_reason="비정기 관계 지출. 고객이 판단할 지출"),
    SpendRule(r"비정기", "미확인", keep_reason="연간 비정기 지출 적립. 줄이면 결제하는 달에 부족해짐"),
)
DEFAULT_RULE = SpendRule(r".*", "미확인", keep_reason="분류 규칙 없음. 거래내역 확인 필요")


def match_rule(label: str) -> SpendRule:
    return next((r for r in SPEND_RULES if re.search(r.pattern, label)), DEFAULT_RULE)


@dataclass(frozen=True)
class PolicyNote:
    """요약본에 담기지 않는 정책 이용 조건. 온통청년 원문에서 확인한 값만 적는다."""
    pattern: str
    conditions: str
    youth_age_max: int | None = None  # 청년 구간 상한 나이
    youth_pct: float | None = None
    general_pct: float | None = None


POLICY_NOTES = (
    PolicyNote(r"K-?패스", "월 15회 이상 대중교통 이용 시 환급. 환급은 이용 다음 달 이후 지급",
               youth_age_max=34, youth_pct=30, general_pct=20),
)


def policy_note(policy_name: str) -> PolicyNote | None:
    return next((n for n in POLICY_NOTES if re.search(n.pattern, policy_name)), None)
