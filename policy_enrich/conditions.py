"""(가) 정책 소득·자격 조건 구조화.

policies 테이블의 income_type / income_min / income_max / income_etc / apply_condition /
support_content 문장에서 소득 상한을 뽑아 숫자 필드로 만든다.
- 중위소득 비율은 '본인(청년)' 기준과 '가구(원가구·부모 포함)' 기준을 나눠 담는다.
- 금액 상한은 본인 연소득, 부부합산 연소득, 월소득, 재산, 건강보험료로 나눈다.
- 원문에 없는 수치는 만들지 않는다. 못 찾으면 NULL로 두고 parse_confidence로 표시한다.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .amounts import AMOUNT_PATTERN, parse_amount

INCOME_TYPE = {"0043001": "무관", "0043002": "연소득", "0043003": "기타"}
PERIOD_TYPE = {"0057001": "특정기간", "0057002": "상시", "0057003": "마감"}

_UPPER = r"(?:이하|이내|미만|까지)"
_MEDIAN_RE = re.compile(
    rf"(?:기준\s*)?중위\s*소득\s*(?:의\s*)?(\d{{2,3}})\s*%\s*({_UPPER}|초과|이상)?"
)
_RANGE_UPPER_RE = re.compile(rf"\s*[~∼-]\s*(\d{{2,3}})\s*%\s*{_UPPER}")
_HOUSEHOLD_HINT = re.compile(r"((?<!독립)가구|원가구|부모|세대|가족|소득인정액)")
_PERSONAL_HINT = re.compile(r"(본인|청년\s*(?:개인|본인)?|개인|독립가구)")

_AMT = rf"({AMOUNT_PATTERN})"
_COUPLE_RE = re.compile(rf"부부\s*합산[^\d\n]{{0,15}}(?:{AMOUNT_PATTERN}\s*[~∼]\s*)?{_AMT}\s*{_UPPER}?")
_ANNUAL_RE = re.compile(
    rf"(?:본인\s*|개인\s*)?(?:연\s*(?:간\s*)?(?:평균\s*)?(?:소득|총급여|근로소득|급여)|연소득|총급여|소득)\s*(?:이|은|금액)?\s*(?:연\s*)?{_AMT}\s*{_UPPER}"
)
_MONTHLY_RE = re.compile(
    rf"월\s*(?:평균\s*)?(?:소득|급여|근로\s*소득)?\s*(?:액)?\s*{_AMT}\s*{_UPPER}"
)
# "청년:연소득 5천만원 이하, 청년 외: 6천만원, 신혼부부 7.5천만원"처럼 대상별 기준이 있으면 청년 기준을 따로 잡는다
_YOUTH_ANNUAL_RE = re.compile(
    rf"[(\[➊①]?\s*청년(?!\s*외)(?!\s*\*?\s*외)[)\]]?\*?\s*[:：]?\s*(?:임차인\s*)?(?:본인\s*)?(?:연\s*소득|연소득)?\s*{_AMT}\s*(?:{_UPPER}|(?=[,，/(\[➋②)]))")
_ASSET_RE = re.compile(rf"(?:재산|자산)[^\d\n]{{0,20}}{_AMT}\s*{_UPPER}")
_HEALTH_RE = re.compile(rf"직장\s*가입자[^\d\n]{{0,10}}{_AMT}")
_EARNED_INCOME_RE = re.compile(r"(근로|사업)\s*[·,ㆍ]?\s*(?:사업)?\s*소득[^\n]{0,15}(?:월\s*)?\d+\s*만\s*원\s*이상|재직\s*중|근로\s*중")


@dataclass
class PolicyCondition:
    policy_id: str
    income_type: str
    income_basis: str  # 무관 / 본인 / 가구 / 본인+가구 / 미상
    personal_median_pct_max: float | None = None
    household_median_pct_max: float | None = None
    annual_income_max_won: int | None = None
    youth_annual_income_max_won: int | None = None  # 대상별 기준 중 '청년' 기준(있을 때만)
    couple_annual_income_max_won: int | None = None
    monthly_income_max_won: int | None = None
    asset_max_won: int | None = None
    health_insurance_max_won: int | None = None
    requires_earned_income: int = 0
    parse_confidence: str = "none"  # high / partial / none / not_applicable
    income_text: str = ""
    recruit_status: str = "unknown"  # open / always / closed / unknown
    apply_end_date: str | None = None
    dedup_key: str = ""

    def as_row(self) -> dict:
        return asdict(self)


def _clean(text) -> str:
    return re.sub(r"[ \t\r]+", " ", str(text or "")).strip()


def _max(current, value):
    if value is None:
        return current
    return value if current is None else max(current, value)


def _amount_upper(regex: re.Pattern, text: str, floor: int = 0) -> int | None:
    best = None
    for m in regex.finditer(text):
        won = parse_amount(m.group(1))
        if won and won > floor:
            best = _max(best, won)
    return best


def parse_median(text: str) -> tuple[float | None, float | None]:
    """(본인 기준 상한 %, 가구 기준 상한 %). 문맥상 가구 단서가 앞 25자 안에 있으면 가구."""
    personal = household = None
    for m in _MEDIAN_RE.finditer(text):
        direction = m.group(2) or ""
        pct = float(m.group(1))
        if direction in ("초과", "이상"):
            # 하한은 자격 상한이 아니다. "50%초과 ~ 100%이하" 구간이면 뒤쪽 상한을 쓴다
            upper = _RANGE_UPPER_RE.match(text, m.end())
            if not upper:
                continue
            pct = float(upper.group(1))
        if not 10 <= pct <= 300:
            continue
        window = text[max(0, m.start() - 25):m.start()]
        # 같은 줄에서 가장 가까운 단서를 따른다
        line_window = window.split("\n")[-1]
        hh = list(_HOUSEHOLD_HINT.finditer(line_window))
        pp = list(_PERSONAL_HINT.finditer(line_window))
        if hh and (not pp or hh[-1].start() > pp[-1].start()):
            household = _max(household, pct)
        elif pp:
            personal = _max(personal, pct)
        else:
            # 단서가 없으면 중위소득은 통상 가구 기준으로 판정한다
            household = _max(household, pct)
    return personal, household


def recruit_status(period_type: str | None, apply_period: str | None,
                   biz_end_date: str | None, today: str) -> tuple[str, str | None]:
    """today는 YYYYMMDD. (상태, 신청 마감일)"""
    end = None
    if apply_period and "~" in apply_period:
        end = re.sub(r"\D", "", apply_period.split("~")[1])[:8] or None
    label = PERIOD_TYPE.get(period_type or "", "")
    biz_end = re.sub(r"\D", "", biz_end_date or "")[:8]
    if label == "마감":
        return "closed", end
    if biz_end and biz_end < today:
        return "closed", end
    if label == "상시":
        return "always", end
    if end:
        return ("open" if end >= today else "closed"), end
    return "unknown", end


_YEAR_PREFIX = re.compile(r"^\s*[\[(（]?\s*(?:19|20)\d{2}\s*(?:년도?|년)?\s*[\])）]?\s*")
_NOISE = re.compile(r"[\s'\"‘’“”·ㆍ,.\-_()\[\]（）「」『』<>]+")


def dedup_key(policy_name: str | None) -> str:
    """연도 접두어와 기호를 떼어낸 정책명. 같은 키 = 같은 사업의 다른 연도/지자체 공고 후보."""
    name = _YEAR_PREFIX.sub("", policy_name or "")
    name = re.sub(r"(모집\s*)?공고$", "", name.strip())
    return _NOISE.sub("", name).lower()


def build_condition(row: dict, today: str) -> PolicyCondition:
    income_type = INCOME_TYPE.get(row.get("income_type") or "", "미상")
    income_etc = _clean(row.get("income_etc"))
    apply_cond = _clean(row.get("apply_condition"))
    support = _clean(row.get("support_content"))
    status, end = recruit_status(row.get("apply_period_type"), row.get("apply_period"),
                                 row.get("biz_end_date"), today)
    cond = PolicyCondition(
        policy_id=str(row["policy_id"]),
        income_type=income_type,
        income_basis="미상",
        recruit_status=status,
        apply_end_date=end,
        dedup_key=dedup_key(row.get("policy_name")),
    )

    # 1) API가 숫자로 준 연소득 상한(만원 단위)
    try:
        api_max = int(row.get("income_max") or 0)
    except ValueError:
        api_max = 0
    if income_type == "연소득" and api_max > 0:
        cond.annual_income_max_won = api_max * 10_000

    # 2) 문장 파싱. income_etc를 우선하고, 소득 단서가 없으면 지원조건·지원내용까지 본다.
    primary = income_etc
    fallback = "\n".join(t for t in (apply_cond, support) if t)
    sources = [primary] if primary else []
    if not _has_income_signal(primary):
        sources.append(fallback)
    text = "\n".join(s for s in sources if s)
    cond.income_text = (income_etc or _income_snippet(fallback))[:300]

    personal, household = parse_median(text)
    cond.personal_median_pct_max = personal
    cond.household_median_pct_max = household
    cond.couple_annual_income_max_won = _amount_upper(_COUPLE_RE, text, floor=1_000_000)
    text_wo_couple = _COUPLE_RE.sub(" ", text)
    cond.annual_income_max_won = _max(cond.annual_income_max_won,
                                      _amount_upper(_ANNUAL_RE, text_wo_couple, floor=5_000_000))
    youth = _YOUTH_ANNUAL_RE.search(text_wo_couple)
    if youth:
        cond.youth_annual_income_max_won = parse_amount(youth.group(1))
    monthly = _amount_upper(_MONTHLY_RE, text_wo_couple, floor=300_000)
    if monthly and monthly < 30_000_000:  # 월 3천만원 이상은 연소득 오인식으로 본다
        cond.monthly_income_max_won = monthly
    cond.asset_max_won = _amount_upper(_ASSET_RE, text, floor=1_000_000)
    cond.health_insurance_max_won = _amount_upper(_HEALTH_RE, text)
    cond.requires_earned_income = int(bool(_EARNED_INCOME_RE.search(text)))

    numeric = [personal, household, cond.annual_income_max_won, cond.youth_annual_income_max_won,
               cond.couple_annual_income_max_won,
               cond.monthly_income_max_won, cond.asset_max_won]
    found = any(v is not None for v in numeric)

    if personal is not None and household is not None:
        cond.income_basis = "본인+가구"
    elif household is not None:
        cond.income_basis = "가구"
    elif found:
        cond.income_basis = "본인"
    elif income_type == "무관" and not _has_income_signal(text):
        cond.income_basis = "무관"

    if cond.income_basis == "무관":
        cond.parse_confidence = "not_applicable"
    elif found and primary and _has_income_signal(primary):
        cond.parse_confidence = "high"
    elif found:
        cond.parse_confidence = "partial"  # 지원내용 등 보조 문장에서 찾음
    else:
        cond.parse_confidence = "none"
    return cond


_INCOME_SIGNAL = re.compile(r"중위\s*소득|연\s*소득|소득\s*인정액|총급여|부부\s*합산|월\s*소득|건강보험료|재산")


def _has_income_signal(text: str) -> bool:
    return bool(text and _INCOME_SIGNAL.search(text))


def _income_snippet(text: str) -> str:
    m = _INCOME_SIGNAL.search(text or "")
    if not m:
        return ""
    return text[max(0, m.start() - 40): m.start() + 160]
