"""고객 문서(선택) → 프로필 → 정책 매칭 → 지출 연결 → 소비 보고서 → step2 → 반영 → 전략 → 에이전트 → plan·대시보드.

사용법:
    python -X utf8 -m planning.pipeline --case kim_doyun --step2 planning/cases/kim_doyun_step2.json --scenario certain
    python -X utf8 -m planning.pipeline --case blank_01            (step2 없이: build_step2로 유도)
    python -X utf8 -m planning.pipeline --case new_01 --docs <고객 문서 폴더>

- 기존 모듈 함수를 불러 쓰고 중간 결과는 메모리로 넘긴다. 정책 DB는 읽기 전용(?mode=ro)으로만 연다.
- 고객 프로필 DB(customer_profiles.db)는 열지 않는다. 정책 판정·소비 보고서가 DB 형태 입력을 받으므로
  프로필을 메모리 SQLite(:memory:)에 넣어 쓴다. module_results 저장은 하지 않는다.
- 쓰는 파일은 output/plan_<case>_<scenario>_v1.json, dashboard_<case>_<scenario>_v1.pdf,
  ai_log_plan_<case>_<scenario>_v1.json 세 개뿐이다(--force 없이 덮어쓰지 않음).
  대시보드는 기본이 PDF이고 --format html이면 HTML, both면 둘 다 쓴다. PDF는 설치된 Edge·Chrome으로 만든다.
- 한 단계가 실패하면 어디서 왜 멈췄는지 한 줄로 알리고 끝낸다.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from customer.policy_match import load_customer, match
from customer.profile import age_on, connect, fact, load_case, store, validate
from customer.spending_handoff import build as build_handoff
from spending_report.inputs import load_input
from spending_report.llm import get_client
from spending_report.report import build_report

from .advisor import advise
from .reflect import SCENARIOS
from .reflect import build as reflect_build
from .spending_cut import spending_cut
from .step2_build import build_step2
from .strategy import build_strategy

ROOT = Path(__file__).resolve().parents[1]
CASE_DIRS = (ROOT / "customer" / "cases", ROOT / "customer" / "cases" / "auto")
POLICY_DB = ROOT / "data" / "youth_policy_enriched.db"
VERSION = "v1"


class StepFailed(RuntimeError):
    def __init__(self, step: str, cause: Exception):
        super().__init__(f"[{step}] 단계에서 멈춤: {type(cause).__name__}: {cause}")
        self.step = step


class Steps:
    def __init__(self):
        self.steps: list[dict] = []

    def run(self, name: str, fn, *, warnings=None):
        started = time.perf_counter()
        try:
            value = fn()
        except SystemExit as exc:  # 기존 모듈이 SystemExit로 알리는 경우
            self.steps.append({"step": name, "status": "failed", "seconds": round(time.perf_counter() - started, 3),
                               "error": str(exc)})
            raise StepFailed(name, RuntimeError(str(exc))) from exc
        except Exception as exc:
            self.steps.append({"step": name, "status": "failed", "seconds": round(time.perf_counter() - started, 3),
                               "error": f"{type(exc).__name__}: {exc}"})
            raise StepFailed(name, exc) from exc
        w = warnings(value) if callable(warnings) else (warnings or [])
        self.steps.append({"step": name, "status": "ok", "seconds": round(time.perf_counter() - started, 3),
                           "warnings": list(w)[:30]})
        return value

    def skip(self, name: str, reason: str):
        self.steps.append({"step": name, "status": "skipped", "seconds": 0.0, "reason": reason})


def find_case(case_id: str) -> Path:
    for d in CASE_DIRS:
        p = d / f"{case_id}.json"
        if p.exists():
            return p
    raise FileNotFoundError(f"케이스 파일 없음: customer/cases/{case_id}.json, customer/cases/auto/{case_id}.json")


def profile_from_documents(case_id: str, folder: Path) -> dict:
    """from_documents.build_case → 임시 파일 → load_case. customer/cases와 DB에는 쓰지 않는다."""
    from customer.from_documents import build_case
    case = build_case(case_id, folder)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{case_id}.json"
        path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
        return load_case(path)


def memory_db(profile: dict) -> sqlite3.Connection:
    conn = connect(":memory:")
    store(conn, profile)
    return conn


def policy_match(profile: dict, policy_db: Path) -> dict:
    if not policy_db.exists():
        raise FileNotFoundError(f"정책 DB 없음: {policy_db}")
    conn = memory_db(profile)
    customer = load_customer(conn, profile["case_id"], profile["profile_version"])
    pconn = sqlite3.connect(f"{policy_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return match(pconn, customer)
    finally:
        pconn.close()
        conn.close()


class UsageRecorder:
    """LLM 클라이언트를 감싸 호출마다 소요 시간과 입력·출력 토큰 수(있으면)를 남긴다."""

    def __init__(self, inner):
        self.inner = inner
        self.records: list[dict] = []
        self.stage = None

    @property
    def provider(self):
        return self.inner.provider

    @property
    def model(self):
        return self.inner.model

    def __getattr__(self, name):  # last_usage·last_role·last_provider·last_model 등은 안쪽 클라이언트 것
        if name.startswith("last_"):
            return getattr(self.inner, name, None)
        raise AttributeError(name)

    def chat(self, messages: list[dict], max_tokens: int = 200) -> str:
        started = time.perf_counter()
        ok = False
        try:
            out = self.inner.chat(messages, max_tokens=max_tokens)
            ok = True
            return out
        finally:
            usage = getattr(self.inner, "last_usage", None) or {}
            self.records.append({"stage": self.stage, "ok": ok,
                                 "role": getattr(self.inner, "last_role", None) or ("primary" if ok else None),
                                 "provider": getattr(self.inner, "last_provider", None) or self.inner.provider,
                                 "model": getattr(self.inner, "last_model", None) or self.inner.model,
                                 "latency_ms": round((time.perf_counter() - started) * 1000),
                                 "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                                 "output_tokens": usage.get("output_tokens", usage.get("completion_tokens"))})


def sentence_sources(report: dict, report_log: list, records: list, advice: dict, advisor_log: list) -> dict:
    """문장마다 누가 썼는지: 주 모델·대체(보조) 모델·템플릿. 소비 보고서 요약·코칭과 에이전트 서술·요약을 센다."""
    counts = {"primary": 0, "secondary": 0, "template": 0}
    sr_records = [r for r in records if r["stage"] == "소비 보고서"]
    role_of = {}
    for entry, rec in zip(report_log, sr_records):  # narrative는 호출마다 기록 한 줄
        if entry.get("accepted"):
            role_of[entry["section"]] = rec.get("role") or "primary"
    narrative = report["narrative"]
    units = [("summary", narrative["summary"])] + [(f"coaching:{c['rec_id']}", c) for c in narrative["coaching"]]
    for section, unit in units:
        counts[role_of.get(section, "primary") if unit["source"] == "llm" else "template"] += 1
    accepted = {}
    for e in advisor_log:
        if e.get("accepted") and e["stage"] in ("narrative", "summary"):
            accepted[(e["stage"], e["term"])] = e.get("role") or "primary"
    for term, t in advice["terms"].items():
        counts[accepted.get(("narrative", term), "primary") if t["source"] == "llm" else "template"] += 1
    s = advice["summary"]
    counts[accepted.get(("summary", "전체"), "primary") if s["source"] == "llm" else "template"] += 1
    return counts


def spending(profile: dict, handoff: dict, client) -> tuple[dict, list]:
    conn = memory_db(profile)
    try:
        conn.execute("INSERT INTO module_results VALUES (?,?,?,?,?,?)",
                     (profile["case_id"], profile["profile_version"], "policy_to_spending", "completed",
                      json.dumps(handoff, ensure_ascii=False), handoff["generated_at"]))
        inp = load_input(conn, profile["case_id"], profile["profile_version"])
        return build_report(inp, client)
    finally:
        conn.close()


# ── plan 조립 ────────────────────────────────────────────────
def customer_summary(profile: dict) -> dict:
    birth = fact(profile, "birth_date")
    return {"case_id": profile["case_id"], "profile_version": profile["profile_version"],
            "as_of": profile["as_of_date"], "is_synthetic": profile.get("is_synthetic"),
            "name": fact(profile, "name"), "age": age_on(birth, profile["as_of_date"]) if birth else None,
            "sex": fact(profile, "sex"), "occupation": fact(profile, "occupation"),
            "employment_status": fact(profile, "employment_status"),
            "household_size": fact(profile, "household_size"), "marital_status": fact(profile, "marital_status"),
            "region_sido": fact(profile, "region_sido"), "region_sigungu": fact(profile, "region_sigungu"),
            "home_owner": fact(profile, "home_owner"), "housing_tenure": fact(profile, "housing_tenure"),
            "risk_profile": fact(profile, "risk_profile")}


def policy_summary(policy: dict, reflected: dict, cut: dict) -> dict:
    refl = reflected["_meta"]["policy_reflection"]
    housing_loans = [{"policy": f["policy_name"], "verdict": f["verdict"],
                      "limit": None if f["benefit"].get("loan_limit_won") is None else round(f["benefit"]["loan_limit_won"] / 10_000, 2),
                      "url": f["apply"].get("url")}
                     for f in policy.get("findings", []) if f["benefit"]["type"] == "대출"]
    return {"generated_at": policy.get("generated_at"),
            "policy_db_as_of": policy.get("search_scope", {}).get("policy_db_as_of"),
            "counts": policy.get("search_scope", {}).get("counts"), "summary": policy.get("summary"),
            "applied": refl["applied_policies"], "other_scenario": refl["other_scenario"],
            "not_reflected": {"one_off_income": cut["separate"]["one_off_income"],
                              "one_off_on_lines": cut["separate"]["one_off_on_lines"],
                              "savings_programs": cut["separate"]["savings_programs"], "loans": housing_loans,
                              "counts": refl["not_reflected"]},
            "to_check": cut.get("to_check", []),
            "questions": policy.get("questions", []), "disclaimer": policy.get("disclaimer")}


def spending_summary(report: dict) -> dict:
    calc = report["calculations"]
    return {"status": report["status"], "effect": calc["effect"], "basis": calc["basis"], "metrics": calc["metrics"],
            "totals": calc["cashflow"]["totals"], "recommendations": len(report["recommendations"]),
            "data_requests": report["data_requests"], "questions": report["questions"],
            "narrative": report["narrative"]["summary"], "ai_usage": report["ai_usage"]}


LIMITATIONS = [
    "가상 고객 자료로 만든 검토용 초안이며 담당자 검토 전입니다. 투자 권유나 상품 추천이 아닙니다.",
    "수익률·물가·위험 비중·대출 금리는 가정값이며 미래 수익을 보장하지 않습니다. 모든 전망은 오늘 가치 기준입니다.",
    "정책 혜택은 입력값 기준 판정이며 기관의 최종 선정과 다릅니다. 일시금·저축·대출 정책은 전망에 넣지 않았습니다.",
    "ISA·연금계좌 세액공제 한도와 공제율은 원문 확인이 필요한 제도값입니다. 세법 변경을 반영하지 않습니다.",
    "DSR은 새 주택 대출만 넣은 간이 계산이며 기존 대출과 금융기관별 기준을 반영하지 않습니다.",
    "미확인 값은 0으로 계산한 부분이 있어 '고객에게 물어볼 것'의 자료를 받으면 다시 계산해야 합니다.",
]


def run_pipeline(case_id: str, *, docs: Path | None = None, step2_path: Path | None = None, scenario: str = "certain",
                 client=None, policy_db: Path = POLICY_DB, goal_basis: str = "customer",
                 inflation: float = 2.0) -> tuple[dict, list]:
    """goal_basis: customer(고객이 적은 재무목표 기준, 기본) 또는 statistical(4대 목표 통계 방식).
    inflation: 연 물가상승률(%). 명목 수익률에서 빼 오늘 가치로 비교한다. 0이면 명목 그대로."""
    if goal_basis not in ("customer", "statistical"):
        raise ValueError(f"goal_basis는 customer 또는 statistical: {goal_basis}")
    akw = {"goal_basis": goal_basis}
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario는 {list(SCENARIOS)} 중 하나: {scenario}")
    client = UsageRecorder(client or get_client())
    st = Steps()
    if docs:
        profile = st.run("문서→프로필", lambda: profile_from_documents(case_id, docs))
        st.skip("프로필 적재", "문서에서 만든 프로필 사용")
    else:
        st.skip("문서→프로필", "--docs 없음")
        profile = st.run("프로필 적재", lambda: load_case(find_case(case_id)),
                         warnings=lambda p: [f"{i['check']}: {i['message']}" for i in validate(p)])
    errors = [i for i in validate(profile) if i["level"] == "error"]
    if errors:
        raise StepFailed("프로필 검증", ValueError("; ".join(i["message"] for i in errors)))
    policy = st.run("정책 매칭", lambda: policy_match(profile, policy_db),
                    warnings=lambda p: [f"정책 DB 기준일 {p['search_scope']['policy_db_as_of']}"])
    handoff = st.run("지출 연결", lambda: build_handoff(policy, profile["cashflow_items"], profile["liabilities"]))
    client.stage = "소비 보고서"
    report, report_log = st.run("소비 보고서", lambda: spending(profile, handoff, client),
                                warnings=lambda r: r[0]["limitations"][:2])
    if step2_path:
        step2 = st.run("step2", lambda: json.loads(Path(step2_path).read_text(encoding="utf-8")),
                       warnings=lambda s: [f"팀원 step2 사용: {Path(step2_path).as_posix()}"])
    else:
        step2 = st.run("step2", lambda: build_step2(profile, report), warnings=lambda s: s["_meta"]["warnings"])
    cut = st.run("정책 절감", lambda: spending_cut(profile, policy, copy.deepcopy(step2), allocate_kwargs=akw), warnings=lambda c: c["warnings"])
    sources = {"step2": Path(step2_path).as_posix() if step2_path else "build_step2(유도)", "case_id": profile["case_id"],
               "profile_version": profile["profile_version"],
               "policy": "정책 DB 판정(읽기 전용, 메모리)",
               "policy_db_as_of": policy.get("search_scope", {}).get("policy_db_as_of")}
    reflected = st.run("반영본 step2", lambda: reflect_build(copy.deepcopy(step2), profile, policy, scenario, sources=sources,
                                                     **akw),
                       warnings=lambda r: r["_allocation"]["warnings"])
    strategy = st.run("전략 엔진", lambda: build_strategy(reflected, profile, policy, cut, goal_basis=goal_basis,
                                                              assumptions={"inflation": inflation / 100}), warnings=lambda s: s["warnings"])
    advisor_log: list = []
    client.stage = "에이전트"
    advice = st.run("에이전트", lambda: advise(strategy, client, log=advisor_log),
                    warnings=lambda a: [f"호출 {a['calls']}, 채택 {a['accepted']}, 거부 {a['rejected']}"])
    inner = client.inner
    advice["fallback"] = inner.summary() if hasattr(inner, "summary") else {"enabled": False}
    advice["sentence_sources"] = sentence_sources(report, report_log, client.records, advice, advisor_log)
    plan = {
        "module": "plan", "version": VERSION, "case_id": profile["case_id"], "scenario": scenario,
        "scenario_label": SCENARIOS[scenario], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "customer": customer_summary(profile),
        "step2": reflected,
        "step2_source": "팀원 step2" if step2_path else "프로필에서 유도(build_step2)",
        "goal_basis": goal_basis, "inflation_pct": inflation,
        "goals_input": strategy.get("goals_input"),
        "policy": policy_summary(policy, reflected, cut),
        "spending": spending_summary(report),
        "strategy": strategy,
        "advice": advice,
        "pipeline": {"steps": st.steps, "llm_provider": client.provider, "llm_model": client.model},
        "limitations": list(LIMITATIONS),
        "review": {"state": "담당자 검토 전", "reviewer": None, "reviewed_at": None},
    }
    ai_log = {"case_id": profile["case_id"], "scenario": scenario, "provider": client.provider, "model": client.model,
              "calls": client.records, "fallback": advice["fallback"], "spending_report": report_log, "advisor": advisor_log}
    return plan, ai_log


def out_paths(out_dir: Path, case_id: str, scenario: str, suffix: str = "", fmt: str = "html") -> dict[str, Path]:
    stem = f"{case_id}_{scenario}_{VERSION}{suffix}"
    paths = {"plan": out_dir / f"plan_{stem}.json"}
    if fmt in ("html", "both"):
        paths["dashboard"] = out_dir / f"dashboard_{stem}.html"
    if fmt in ("pdf", "both"):
        paths["dashboard_pdf"] = out_dir / f"dashboard_{stem}.pdf"
    paths["ai_log"] = out_dir / f"ai_log_plan_{stem}.json"
    return paths


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", required=True)
    parser.add_argument("--docs", type=Path)
    parser.add_argument("--step2", type=Path)
    parser.add_argument("--scenario", default="certain", choices=list(SCENARIOS))
    parser.add_argument("--out-dir", default=ROOT / "output", type=Path)
    parser.add_argument("--policy-db", default=POLICY_DB, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--inflation", type=float, default=2.0,
                        help="연 물가상승률(%%). 명목 수익률에서 빼 오늘 가치로 비교. 0이면 명목 그대로(기본 2.0)")
    parser.add_argument("--goal-basis", choices=["customer", "statistical"], default="customer",
                        help="customer: Fact Finding 2번 재무목표 기준(기본), statistical: 4대 생애목표 통계 방식")
    parser.add_argument("--llm-provider", choices=["none", "local", "server", "anthropic"],
                        help="기본은 환경변수 LLM_PROVIDER(없으면 none). 키는 LLM_API_KEY 환경변수로만")
    parser.add_argument("--llm-model", help="기본은 LLM_MODEL")
    parser.add_argument("--llm-base-url", help="기본은 LLM_BASE_URL")
    parser.add_argument("--llm-fallback-provider", choices=["none", "local", "server", "anthropic"],
                        help="주 모델이 실패·소진되면 쓸 보조 공급자. 기본은 LLM_FALLBACK_PROVIDER(없으면 none)")
    parser.add_argument("--llm-fallback-model", help="기본은 LLM_FALLBACK_MODEL(없으면 Qwen/Qwen3-4B-Instruct-2507)")
    parser.add_argument("--llm-fallback-base-url", help="기본은 LLM_FALLBACK_BASE_URL(없으면 http://localhost:8080/v1)")
    parser.add_argument("--format", choices=["pdf", "html", "both"], default="pdf",
                        help="대시보드 형식. pdf(기본)는 설치된 Edge·Chrome으로 인쇄, html은 브라우저 없이 저장")
    parser.add_argument("--suffix", default="", help="산출물 파일명 끝에 붙임(예: _llm). 템플릿 판을 덮어쓰지 않게")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[A-Za-z0-9_\-]*", args.suffix):
        print("--suffix에는 영문·숫자·_·-만 쓸 수 있음", file=sys.stderr)
        return 2
    paths = out_paths(args.out_dir, args.case, args.scenario, args.suffix, args.format)
    existing = [p.name for p in paths.values() if p.exists()]
    if existing and not args.force:
        print(f"이미 있음(덮어쓰지 않음, --force로 덮어씀): {', '.join(existing)}", file=sys.stderr)
        return 2
    try:
        try:
            client = get_client(args.llm_provider, args.llm_model, args.llm_base_url,
                                fallback_provider=args.llm_fallback_provider, fallback_model=args.llm_fallback_model,
                                fallback_base_url=args.llm_fallback_base_url)
        except ValueError as exc:
            raise StepFailed("LLM 설정", exc) from exc
        plan, ai_log = run_pipeline(args.case, docs=args.docs, step2_path=args.step2, scenario=args.scenario,
                                    policy_db=args.policy_db, client=client, goal_basis=args.goal_basis,
                                    inflation=args.inflation)
        from .dashboard import render
        started = time.perf_counter()
        try:
            html = render(plan)
        except Exception as exc:
            raise StepFailed("대시보드", exc) from exc
        plan["pipeline"]["steps"].append({"step": "대시보드", "status": "ok",
                                          "seconds": round(time.perf_counter() - started, 3), "warnings": []})
        html = render(plan)  # 대시보드 단계 기록을 포함해 다시 그림
        if "dashboard_pdf" in paths:
            from .pdf import PdfError, find_browser, html_to_pdf
            try:
                find_browser()  # 없으면 아무 파일도 쓰기 전에 멈춤
            except PdfError as exc:
                raise StepFailed("PDF 변환", exc) from exc
    except StepFailed as exc:
        print(str(exc).splitlines()[0], file=sys.stderr)
        return 1
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if "dashboard_pdf" in paths:
        try:
            html_to_pdf(html, paths["dashboard_pdf"])
        except PdfError as exc:
            print(str(StepFailed("PDF 변환", exc)).splitlines()[0], file=sys.stderr)
            return 1
    paths["plan"].write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if "dashboard" in paths:
        paths["dashboard"].write_text(html, encoding="utf-8")
    paths["ai_log"].write_text(json.dumps(ai_log, ensure_ascii=False, indent=2), encoding="utf-8")
    s = plan["strategy"]["summary"]
    print(json.dumps({"case": plan["case_id"], "scenario": args.scenario, "step2": plan["step2_source"],
                      "files": [p.as_posix() for p in paths.values()],
                      "surplus": [s["surplus_before"], s["surplus_after"]], "monthly_need": s["monthly_need"],
                      "monthly_shortfall": s["monthly_shortfall"], "cards": len(plan["strategy"]["cards"]),
                      "warnings": len(plan["strategy"]["warnings"]),
                      "llm": {k: plan["advice"][k] for k in ("provider", "calls", "accepted", "rejected")}},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
