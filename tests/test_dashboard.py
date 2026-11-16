"""planning.dashboard 시험. 정책 DB로 김도윤 plan을 만든 뒤 HTML을 검사하고, output의 7개 plan도 다시 그려 본다."""
import copy
import html as htmllib
import json
import re

import pytest

from planning.dashboard import line_chart, man, render, stacked_bar
from planning.pipeline import POLICY_DB, ROOT, run_pipeline
from spending_report.llm import NoLLM

SECTIONS = ["head", "kpi", "goals", "allocation", "phases", "assets", "retire", "strategy", "projections", "decisions",
            "cashflow", "options", "policy", "appendix"]
OUTPUT_PLANS = [f"plan_kim_doyun_{s}_v1.json" for s in ("certain", "potential")] + \
               [f"plan_{c}_certain_v1.json" for c in ("blank_01", "blank_02", "blank_03", "blank_04", "lee_seojun")] +                ["plan_kim_doyun_certain_v1_stat.json"]
# 화면에 남으면 안 되는 코드 표현: 파이썬 값, 리스트 표기, 모듈·필드 이름
CODE_RE = re.compile(r"\bNone\b|\bnull\b|\bTrue\b|\bFalse\b|\[\[|\['|\[\d|\{'|"
                     r"\b(certain|potential|step2|need|gap|cut_monthly|spending_cut|allocation|asset_mapping|reference|"
                     r"profile|deltaM|deltap|deltaq|deltar|a_monthly|emergency|housing|wedding|retire|child|unassigned|"
                     r"liabilities|insurance_contracts|cashflow_items|income_items|tax_items|glide|policy_id|"
                     r"data_requests|monthly_won)\b")


def visible_text(page: str) -> str:
    page = re.sub(r"<style.*?</style>|<script.*?</script>", "", page, flags=re.S)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", page)))


@pytest.fixture(scope="module")
def plan():
    if not POLICY_DB.exists():
        pytest.skip("정책 DB 없음")
    p, _ = run_pipeline("kim_doyun", step2_path=ROOT / "planning" / "cases" / "kim_doyun_step2.json", client=NoLLM())
    return p


def test_sections_in_order(plan):
    page = render(plan)
    pos = [page.find(f'id="{s}"') for s in SECTIONS]
    assert all(p > 0 for p in pos) and pos == sorted(pos)
    for term in ("단기", "중기", "장기"):
        assert f'id="term-{term}"' in page
    assert "담당자 검토 전" in page and "가상 고객" in page
    assert '<nav class="jump"' in page and page.find('<nav class="jump"') < page.find('id="kpi"')
    assert "분배 단계(수익률 미반영) 월 부족" in page and "하한~상한 수익률" in page
    assert page.count('<details class="card">') == len(plan["strategy"]["cards"])
    assert page.count("<polyline") == len(plan["strategy"]["goals"])  # 기준선
    assert page.count("<polygon") == len(plan["strategy"]["goals"])  # 하한~상한 띠


def test_no_external_resources_and_one_inline_script(plan):
    page = render(plan)
    assert not re.search(r"<script[^>]*\bsrc=", page, re.I)
    assert page.count("<script") == 1 and "beforeprint" in page
    assert not re.search(r"<link[^>]+href=[\"']?https?:", page, re.I)
    assert "@import" not in page
    assert not re.search(r"src=[\"']?https?:", page, re.I)
    assert "—" not in page


def test_user_values_are_escaped(plan):
    p = copy.deepcopy(plan)
    p["customer"]["name"] = "<script>alert(1)</script>"
    p["customer"]["occupation"] = '"><img src=x onerror=alert(1)>'
    page = render(p)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page


def test_emergency_card_splits_deposit_and_investment(plan):
    s1 = next(c for c in plan["strategy"]["cards"] if c["id"] == "S1")
    n = s1["numbers"]
    assert n["즉시 인출 예금성(만원)"] + n["즉시 인출 투자자산·가격 변동(만원)"] == pytest.approx(n["즉시 인출 자산(만원)"])
    assert n["즉시 인출 예금성(만원)"] == 500  # 김도윤: 입출금 500, ETF 2,000
    assert n["안전 자산 배정(만원)"] == 1200 and n["즉시 인출 안전 자산 배정(만원)"] == 500
    assert n["즉시 인출 안전 자산 부족(만원)"] == 700 and n["안전 전환 필요(만원)"] == 0
    assert "안전 자산 입출금 500만원 + 정기예금 700만원으로 채웁니다" in s1["action"]
    assert "투자 자산은 비상금에 쓰지 않습니다" in s1["action"] and "700만원이 모자랍니다" in s1["action"]
    assert any("정기예금(만기 2027-04-01)은 만기 전 중도해지 조건 확인" in x for x in s1["confirm"])


def test_child_transition_display(plan):
    text = visible_text(render(plan))
    assert "전환 시점(2028-04) 준비금 1,353.4만원(월 양육비 21.2개월분)" in text
    assert "준비금 기준 1,353.4" in text and "필요자금 19,621.6" not in text
    head = text[text.index("목표별 분배와 수익률 가정"):text.index("단계별 비율 변화")]
    assert "전환 대상" in head and not re.search(r"(?<![\d.])6\.9%", head)
    kpi = text[text.index("핵심 지표"):text.index("고객 재무목표")]
    assert "하한~상한 수익률" in kpi and "양육 전환 뒤" in kpi


@pytest.mark.parametrize("name", OUTPUT_PLANS)
def test_output_dashboards_show_no_code_terms(name):
    path = ROOT / "output" / name
    if not path.exists():
        pytest.skip(f"{name} 없음")
    page = render(json.loads(path.read_text(encoding="utf-8")))
    text = visible_text(page)
    hits = [text[max(0, m.start() - 30):m.end() + 10] for m in CODE_RE.finditer(text)]
    assert not hits, hits[:5]
    saved = ROOT / "output" / name.replace("plan_", "dashboard_").replace(".json", ".html")
    if saved.exists():  # 저장된 대시보드도 같은 기준
        assert not CODE_RE.search(visible_text(saved.read_text(encoding="utf-8")))


def test_blank_customer_renders():
    if not POLICY_DB.exists():
        pytest.skip("정책 DB 없음")
    p, _ = run_pipeline("blank_03", client=NoLLM())
    page = render(p)
    assert 'id="strategy"' in page and "미확인" in page


def test_helpers():
    assert man(1234.5) == "1,234.5" and man(None) == "미확인" and man(10.0) == "10"
    bars = stacked_bar([{"label": "a", "value": 30}, {"label": "b", "value": 70}, {"label": "c", "value": None}])
    assert [b["label"] for b in bars] == ["a", "b"] and bars[1]["pct"] == pytest.approx(70)
    series = [{"year": i, "label": 2026 + i, "base": i * 10.0, "low": i * 8.0, "high": i * 12.0} for i in range(32)]
    ch = line_chart({"need": 100, "series": series})
    labels = [t["label"] for t in ch["xticks"]]
    assert labels[-1] == "2057" and "2056" not in labels  # 끝 연도와 겹치는 눈금은 뺌
    xs = [t["x"] for t in ch["xticks"]]
    assert min(b - a for a, b in zip(xs, xs[1:])) > 40
    assert ch["need_label"] == "필요자금 100" and ch["need_y"] < ch["y_base"]


def test_customer_goal_tables(plan):
    text = visible_text(render(plan))
    assert "고객 재무목표" in text and "결혼자금(본인 부담 희망액)" in text
    assert "기본 목표로 분배" in text and "기본 목표(고객 미기재, 통계값): 양육" in text
    assert "고객이 적은 재무목표 기준" in text
    assert "추가 목표(고객이 더 적은 것)" not in text  # 추가 목표가 없으면 그리지 않음


def test_new_sections_content(plan):
    text = visible_text(render(plan))
    # 수익률 가정과 물가 차감
    assert "명목 6.5%, 물가 2% 차감 후 실질 4.41%" in text and "투자성향은 계산에 쓰지 않았습니다" in text
    # 자산 배정: 안전/투자/보존 구분, 보존자산 합계, 전환 필요 없음
    assert "안전 자산 2,700만원, 투자 자산 2,000만원" in text and "보존자산 합계" in text
    assert "전환 필요 없음" in text and "주택청약 (주거 재원, 연 납입 120)" in text
    # 노후 단계 띠와 표
    assert "1단계 70:30" in text and "2단계 50:50" in text and "3단계 30:70" in text
    assert "25~20년 구간은 70:30으로 처리(확인 필요)" in text
    # 적용 원칙(가이드 문구 그대로)과 출처
    assert "P02 보호자금 우선" in text and "전략 원칙 출처: 팀 예시 보고서 요약" in text
    guide = {p["id"]: p["text"] for p in plan["strategy"]["guide"]["principles"]}
    assert guide["P02"] in text


def test_extra_goal_section_when_customer_wrote_one(plan):
    p = copy.deepcopy(plan)
    goal = copy.deepcopy(p["strategy"]["goals"][0])
    goal.update(goal="other", name="기타 목표 1", kind="extra", label="1년에 한 번 해외여행", recurrence="annual",
                recurring_monthly=25.0, need=300.0)
    p["strategy"]["goals"].append(goal)
    text = visible_text(render(p))
    assert "추가 목표(고객이 더 적은 것): 기타 목표 1(1년에 한 번 해외여행)" in text and "매년 반복 월 25" in text
    assert not CODE_RE.search(text)


def test_transition_section(plan):
    text = visible_text(render(plan))
    assert "단계별 비율 변화" in text and "양육 전환 2028-04(통계 기본값(확인 필요))" in text
    assert "1단계" in text and "6단계" in text and "노후 적립 완료" in text and "자녀 19세(양육비 종료)" in text
    assert "적립 완료 2038-08" in text and "목표 시점에 필요자금 도달 예상" in text
    assert "시점 도래" in text and "지연 달성 2049-03" in text and "추가 노후 재원" in text
    assert "사건 시점은 기준 수익률 경로로 정하고" in text
    assert "산정: 세전 월소득 480만원(총급여 ÷ 12) → 400~499만원 구간" in text
    assert "법원 원문 표로 전체 대조 필요" in text and "양육 전환 뒤" in text


def test_retire_tax_subsection(plan):
    text = visible_text(render(plan))
    assert "노후 재원 계좌 배분과 수령 세금 구간" in text
    for w in ("세제 적격(연금저축·IRP)", "비과세 저축성 보험", "ISA", "일반 계좌", "은퇴 뒤 연 수령", "한도 이내"):
        assert w in text
    assert "적격 계좌 연 수령 1,500 / 1,500" in text and "일반 계좌 이자·배당" in text
    assert "원문 확인 필요" in text and "세무 자문이 아닙니다" in text
    assert text.index("노후 단계별 운용") < text.index("노후 재원 계좌 배분과 수령 세금 구간")
