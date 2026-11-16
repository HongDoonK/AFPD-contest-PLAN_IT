"""(나) 정책 혜택 금액 구조화.

정책명·지원내용 문장에서 혜택 유형과 금액(월액, 기간, 총액, 매칭 비율, 대출 한도, 이자 지원율)을 뽑는다.
여유 자금 계획 에이전트가 "이 돈을 정책 상품에 넣으면 얼마가 붙는가"를 계산할 수 있게 하는 것이 목적이다.
원문에 없는 수치는 만들지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .amounts import AMOUNT_PATTERN, format_won, parse_amount

_AMT = rf"({AMOUNT_PATTERN})"

# 위에서부터 먼저 맞는 유형을 쓴다. 매칭저축이 현금지원보다 앞서야 "30만원 지원"에 먹히지 않는다.
_TYPE_RULES = [
    # 본문에서는 '저축·납입'과 '매칭·적립'이 함께 나올 때만 매칭저축으로 본다(1:1 멘토링, 기업 매칭 오탐 방지)
    ("매칭저축", re.compile(r"(저축\s*계좌|저축\s*통장|희망\s*통장|디딤돌\s*통장|청년\s*통장|적금|도약\s*계좌|자산\s*형성"
                            r"|(?:저축|납입|적립)[^\n]{0,40}(?:매칭|장려금|지원금))")),
    # 우대금리·소득공제가 붙는 본인 저축 상품(정부 매칭 없음)
    ("저축상품", re.compile(r"(청약\s*(?:통장|저축)|우대\s*금리\s*(?:적금|예금|저축))")),
    ("이자지원", re.compile(r"(이자\s*(?:지원|보전)|이차\s*보전|대출\s*이자)")),
    ("보증료지원", re.compile(r"보증료")),
    ("대출", re.compile(r"((?<!도서)대출|융자|대부)")),
    ("월세·주거비", re.compile(r"(월세|임차료|임대료|주거비|주거급여|임대보증금)")),
    # 쓴 돈의 일부를 깎거나 돌려주는 혜택(교통 환급, 문화 패스, 관람료 할인 등)
    ("할인·환급", re.compile(r"(패스(?!트)|동행\s*카드|컬처|할인|환급|감면)")),
    ("현금지원", re.compile(r"(수당|장려금|지원금|활동비|생활비|정착금|정착\s*지원|장학금|창작\s*지원|지급"
                            r"|구입비|응시료|취득\s*비용?|관람비|수강료|가입비|축하금)")),
    ("바우처·현물", re.compile(r"(바우처|이용권|포인트|상품권|현물|교통비|지역\s*화폐|사랑\s*카드|페이)")),
    ("교육·서비스", re.compile(r"(교육|컨설팅|상담|멘토링|프로그램|훈련|조사|연구|운영)")),
]
# 본문 키워드만으로 금융 혜택 유형을 붙이려면 본문에 금액이 하나는 있어야 한다(실태조사·상담 오분류 방지)
_FINANCIAL_TYPES = {"매칭저축", "저축상품", "이자지원", "보증료지원", "대출", "월세·주거비", "할인·환급", "현금지원",
                    "바우처·현물"}

# 지출액 대비 비율 혜택: "청년층 : 30%" 같은 대상별 비율이 있으면 청년 비율을 먼저 쓴다
_YOUTH_PCT_RE = re.compile(r"청년(?!\s*외)[^\d\n%]{0,6}?(\d{1,2}(?:\.\d+)?)\s*%")
_DISCOUNT_PCT_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%[^\n\d]{0,8}?(?:할인|환급|감면|지원)")
_DISCOUNT_WON_RE = re.compile(rf"{_AMT}\s*(?:을|를)?\s*(?:할인|환급)")

_OWN_SAVING_RE = re.compile(
    rf"(?:본인\s*)?(?:저축(?:액)?|납입(?:액)?|적립)\s*(?:액)?\s*[(（]?\s*(?:월\s*)?{_AMT}"
    rf"|(?:매월|월)\s*{_AMT}\s*(?:의|을|를)?\s*(?:적금\s*)?(?:저축|납입|적립)")
_SAVE_THEN_MATCH_RE = re.compile(
    rf"(?:매월|월)\s*{_AMT}\s*(?:이상\s*)?(?:을|를)?\s*(?:저축|납입|적립)[^\n]{{0,40}}?(?:월\s*)?{_AMT}\s*(?:을|를)?\s*(?:지원|적립|매칭)")
_RATIO_RE = re.compile(r"1\s*:\s*(\d+(?:\.\d+)?)\s*(?:매칭|비율)?")
_SAME_AMOUNT_RE = re.compile(r"동일\s*(?:금액|액)")
_MONTHLY_RE = re.compile(rf"(?:매월|월)\s*(?:임차료|임대료|월세|평균|지원금)?\s*(?:최대\s*)?{_AMT}|{_AMT}\s*/\s*월")
_MONTHLY_TIMES_RE = re.compile(rf"월\s*{_AMT}\s*[(（]?\s*(\d{{1,3}})\s*회")  # "월 50만원(60회)"
_TIMES_MONTHS_RE = re.compile(rf"{_AMT}\s*[*xX×]\s*(\d{{1,2}})\s*개월")
_PER_TIME_RE = re.compile(rf"1\s*회\s*(?:당\s*)?{_AMT}[^\n]{{0,30}}?최대\s*(\d{{1,2}})\s*회")
# "24개월 이상 아동", "0~23개월 아동"처럼 나이를 뜻하는 개월은 지원 기간이 아니다
_MONTHS_RE = re.compile(r"(?<![~∼\d])(?:최대\s*)?(\d{1,2})\s*개월(?!\s*(?:이상|미만|이하|아동|영아|된|이내\s*(?:아동|영아)))")
_YEARS_RE = re.compile(r"(\d)\s*년\s*(?:간|동안|만기)|최대\s*(\d)\s*년(?!\s*(?:이상|이하|미만))")
_TOTAL_RE = re.compile(rf"(?<!월)(?<!월\s)(?:최대|총|연간?)\s*{_AMT}")  # "월 최대 N원"은 월액이지 총액이 아니다
_LOAN_LIMIT_RE = re.compile(
    rf"(?:대출|융자|보증)?\s*한도[^\d\n]{{0,15}}?(?:최대\s*)?{_AMT}"
    rf"|(?:보증금|대출금|대출\s*잔액|전세\s*자금)\s*[(（]?\s*최대\s*{_AMT}"
    rf"|{_AMT}\s*(?:까지|이내)\s*(?:대출|융자)")
_RATE_PCT_RE = re.compile(
    r"(?:연\s*)?(\d+(?:\.\d+)?)\s*%\s*(?:p|포인트)?\s*(?:를|을)?\s*(?:이내\s*)?(?:이자|이차|금리)?\s*(?:지원|보전)"
    r"|이자\s*(?:지원|보전)\s*[(（]\s*(?:최대\s*)?(\d+(?:\.\d+)?)\s*%")

# 자격 조건에 쓰인 금액(소득·보증금 상한 등)은 혜택 금액 범위에서 뺀다
_CONDITION_BEFORE = re.compile(r"(소득|보증금|재산|자산|매출|급여|임금|연봉|가격|주택|전세|월세|부채|대출금)[^\d\n]{0,12}$")
_CONDITION_AFTER = re.compile(r"^\s*(이하|미만|초과|이상)")
_ANY_AMOUNT_RE = re.compile(_AMT)


@dataclass
class PolicyBenefit:
    policy_id: str
    benefit_type: str = "기타"
    monthly_amount_won: int | None = None
    duration_months: int | None = None
    total_amount_won: int | None = None
    amount_min_won: int | None = None   # 본문에 나온 혜택 금액 범위(대상별·차수별로 다를 때)
    amount_max_won: int | None = None
    own_saving_monthly_won: int | None = None
    match_monthly_won: int | None = None
    match_ratio: float | None = None
    loan_limit_won: int | None = None
    interest_support_pct: float | None = None
    discount_pct: float | None = None   # 지출액의 N% 할인·환급·지원(K-패스 청년 30%, 응시료 50% 등)
    discount_won: int | None = None     # 1회·1건 정액 할인(기후동행카드 7천원 등)
    summary_line: str = ""
    parse_confidence: str = "none"  # high / partial / none

    def as_row(self) -> dict:
        return asdict(self)


def _clean(text) -> str:
    return re.sub(r"[ \t\r]+", " ", str(text or "")).strip()


def _first(regex: re.Pattern, text: str, lo: int = 1, hi: int = 10**12) -> int | None:
    for m in regex.finditer(text):
        group = next((g for g in m.groups() if g), None)
        won = parse_amount(group) if group else None
        if won and lo <= won <= hi:
            return won
    return None


def benefit_amounts(text: str, lo: int = 10_000, hi: int = 1_000_000_000) -> list[int]:
    """자격 조건 금액을 뺀 혜택 금액 후보들(등장 순서)."""
    amounts = []
    for m in _ANY_AMOUNT_RE.finditer(text):
        before = text[max(0, m.start() - 15):m.start()]
        after = text[m.end():m.end() + 4]
        if _CONDITION_BEFORE.search(before):
            continue
        if _CONDITION_AFTER.match(after) and not re.search(r"(최대|한도|지원)", before):
            continue
        won = parse_amount(m.group(1))
        if won and lo <= won <= hi:
            amounts.append(won)
    return amounts


def classify(name: str, text: str) -> str:
    # 정책명이 더 정확하므로 먼저 본다
    for label, regex in _TYPE_RULES:
        if regex.search(name or ""):
            return label
    has_amount = bool(benefit_amounts(text))
    for label, regex in _TYPE_RULES:
        if label in _FINANCIAL_TYPES and not has_amount:
            continue
        if regex.search(text or ""):
            return label
    return "기타"


def _duration(text: str) -> int | None:
    m = _MONTHS_RE.search(text)
    if m and 1 <= int(m.group(1)) <= 60:
        return int(m.group(1))
    y = _YEARS_RE.search(text)
    years = int(next(g for g in y.groups() if g)) if y else 0
    if 1 <= years <= 10:
        return years * 12
    return None


def build_benefit(row: dict) -> PolicyBenefit:
    name = _clean(row.get("policy_name"))
    text = _clean(row.get("support_content"))
    b = PolicyBenefit(policy_id=str(row["policy_id"]))
    b.benefit_type = classify(name, text)
    b.duration_months = _duration(text)

    if b.benefit_type == "매칭저축":
        pair = _SAVE_THEN_MATCH_RE.search(text)
        if pair:
            b.own_saving_monthly_won = parse_amount(pair.group(1))
            b.match_monthly_won = parse_amount(pair.group(2))
        else:
            b.own_saving_monthly_won = _first(_OWN_SAVING_RE, text, 10_000, 2_000_000)
        ratio = _RATIO_RE.search(text)
        if ratio:
            b.match_ratio = float(ratio.group(1))
        elif _SAME_AMOUNT_RE.search(text):
            b.match_ratio = 1.0
        elif b.own_saving_monthly_won and b.match_monthly_won:
            b.match_ratio = round(b.match_monthly_won / b.own_saving_monthly_won, 2)
        if b.match_monthly_won is None and b.match_ratio and b.own_saving_monthly_won:
            b.match_monthly_won = int(b.own_saving_monthly_won * b.match_ratio)
        b.monthly_amount_won = b.match_monthly_won
    elif b.benefit_type in ("대출", "보증료지원", "이자지원"):
        # 500만원 미만 '한도'는 대출 한도가 아니라 이자·보증료 지원 상한인 경우가 대부분이다
        b.loan_limit_won = _first(_LOAN_LIMIT_RE, text, 5_000_000)
        rate = _RATE_PCT_RE.search(text)
        pct = float(next(g for g in rate.groups() if g)) if rate else None
        if pct is not None and pct <= 10:
            b.interest_support_pct = pct
        if b.benefit_type == "대출":
            b.duration_months = None  # 대출 본문의 개월은 거치·상환 기간이라 혜택 기간이 아니다
        else:
            b.monthly_amount_won = _first(_MONTHLY_RE, text, 10_000, 5_000_000)
    else:
        monthly_times = _MONTHLY_TIMES_RE.search(text)
        times_months = _TIMES_MONTHS_RE.search(text)
        if monthly_times:
            b.monthly_amount_won = parse_amount(monthly_times.group(1))
            b.duration_months = int(monthly_times.group(2))
        else:
            b.monthly_amount_won = _first(_MONTHLY_RE, text, 10_000, 5_000_000)
        if b.monthly_amount_won is None and times_months:
            b.monthly_amount_won = parse_amount(times_months.group(1))
            b.duration_months = int(times_months.group(2))

    if b.benefit_type not in ("대출", "이자지원", "보증료지원", "매칭저축", "저축상품"):
        pct = _YOUTH_PCT_RE.search(text) or _DISCOUNT_PCT_RE.search(text)
        if pct and 1 <= float(pct.group(1)) <= 100:
            b.discount_pct = float(pct.group(1))
        b.discount_won = _first(_DISCOUNT_WON_RE, text, 1_000, 1_000_000)

    # 대출 상품의 금액은 한도이므로 받는 돈 범위로 쓰지 않는다
    amounts = [] if b.benefit_type == "대출" else [a for a in benefit_amounts(text) if a != b.loan_limit_won]
    if amounts:
        b.amount_min_won, b.amount_max_won = min(amounts), max(amounts)

    # '최대 N원'은 자격 조건 금액이 아닌 혜택 금액일 때만 총액으로 인정한다
    explicit_total = _first(_TOTAL_RE, text, 10_000, 500_000_000)
    if explicit_total not in amounts or explicit_total == b.loan_limit_won:
        explicit_total = None
    per_time = _PER_TIME_RE.search(text)
    if b.monthly_amount_won and b.duration_months:
        computed = b.monthly_amount_won * b.duration_months
        # 과정별로 기간이 다를 때 첫 과정만 계산되므로, 원문의 '최대'가 더 크면 그것을 쓴다
        b.total_amount_won = max(computed, explicit_total or 0)
    elif per_time:
        b.total_amount_won = parse_amount(per_time.group(1)) * int(per_time.group(2))
    elif b.benefit_type != "대출" and explicit_total:
        b.total_amount_won = explicit_total
    elif (b.benefit_type not in ("대출", "매칭저축") and b.monthly_amount_won is None
          and len(set(amounts)) == 1):
        b.total_amount_won = amounts[0]  # 본문에 혜택 금액이 하나뿐이면 그것이 일시금이다

    key_fields = [b.monthly_amount_won, b.total_amount_won, b.loan_limit_won,
                  b.interest_support_pct, b.match_ratio, b.discount_pct, b.discount_won]
    filled = sum(v is not None for v in key_fields)
    if filled >= 2 or (b.benefit_type == "매칭저축" and b.match_ratio and b.own_saving_monthly_won):
        b.parse_confidence = "high"
    elif filled == 1:
        b.parse_confidence = "partial"
    elif b.amount_max_won:
        b.parse_confidence = "partial"  # 금액은 있으나 대상별로 갈려 하나로 못 정함
    b.summary_line = summarize(b)
    return b


def summarize(b: PolicyBenefit) -> str:
    parts = []
    if b.benefit_type == "매칭저축" and b.own_saving_monthly_won:
        line = f"월 {format_won(b.own_saving_monthly_won)} 저축 시"
        if b.match_monthly_won:
            line += f" 월 {format_won(b.match_monthly_won)} 매칭"
        elif b.match_ratio:
            line += f" 1:{b.match_ratio:g} 매칭"
        parts.append(line)
    elif b.monthly_amount_won:
        parts.append(f"월 {format_won(b.monthly_amount_won)}")
    if b.duration_months:
        parts.append(f"{b.duration_months}개월")
    if b.total_amount_won:
        parts.append(f"최대 {format_won(b.total_amount_won)}")
    elif b.amount_max_won and b.amount_min_won != b.amount_max_won:
        parts.append(f"대상별 {format_won(b.amount_min_won)}~{format_won(b.amount_max_won)}")
    if b.loan_limit_won:
        parts.append(f"한도 {format_won(b.loan_limit_won)}")
    if b.interest_support_pct:
        parts.append(f"이자 {b.interest_support_pct:g}% 지원")
    if b.discount_pct:
        parts.append(f"지출의 {b.discount_pct:g}%")
    if b.discount_won:
        parts.append(f"{format_won(b.discount_won)} 할인")
    return f"[{b.benefit_type}] " + (", ".join(parts) if parts else "금액 정보 없음(공고 확인)")
