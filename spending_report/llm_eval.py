"""LLM 문장 실험. 같은 고객 입력으로 모델별 보고서를 만들어 검증 통과율, 거절 사유, 지연을 비교한다.

OpenAI 호환 서버의 주소와 키는 환경변수로만 받는다. GPU 서버가 없으면 Colab을 쓴다(colab/README.md).

    LLM_BASE_URL=https://<서버 주소>/v1 LLM_API_KEY=... python -X utf8 -m spending_report.llm_eval \
        --models Qwen/Qwen3-4B-Instruct-2507 Qwen/Qwen2.5-7B-Instruct --label baseline

Anthropic Messages 형식 게이트웨이(학교 게이트웨이 등)는 --provider anthropic:

    LLM_BASE_URL=https://<host>/v1/gateway/claude LLM_API_KEY=... python -X utf8 -m spending_report.llm_eval \\
        --provider anthropic --models claude-sonnet-5-5 --cases kim_doyun --label sonnet_kim

결과는 output/llm_eval/<label>/에 남는다. 모델별 폴더에 보고서(JSON·HTML)와 ai_log를, 위에는
summary.json, summary.md(표), samples.md(문장 비교)를 쓴다. 고객 프로필 DB에는 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import statistics
import time
import zlib
from collections import Counter
from datetime import datetime
from pathlib import Path

from .inputs import ReportInput, load_input
from .llm import DEFAULT_MESSAGES_PATH, DEFAULT_MODEL, AnthropicLLM, LLMUnavailable, ServerLLM, with_fallback
from .render import to_html
from .report import build_report

DEFAULT_CASES = ["kim_doyun", "lee_seojun", "blank_01", "blank_02", "blank_03", "blank_04"]
LICENSES = {
    "Qwen/Qwen2.5-3B-Instruct": "Qwen Research(비상업)",
    "Qwen/Qwen2.5-1.5B-Instruct": "Apache-2.0",
    "Qwen/Qwen3-4B-Instruct-2507": "Apache-2.0",
    "Qwen/Qwen2.5-7B-Instruct": "Apache-2.0",
    "Qwen/Qwen2.5-14B-Instruct": "Apache-2.0",
    "Qwen/Qwen3-1.7B": "Apache-2.0",
    "kakaocorp/kanana-1.5-2.1b-instruct-2505": "Apache-2.0",
    "claude-sonnet-5-5": "상용 API(게이트웨이)",
}
COUNTS = ("sections", "llm", "first_pass", "calls", "identical_retry", "fallback_calls")
TIMING_KEYS = ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms")  # llama.cpp 서버가 재는 값
USAGE_KEYS = ("generation_ms", "completion_tokens", *TIMING_KEYS)
TOKEN_KEYS = ("input_tokens", "output_tokens")  # OpenAI(prompt/completion)·Anthropic(input/output) 형식을 합친 값
LIST_KEYS = ("latency_ms", "context_tokens", *USAGE_KEYS, *TOKEN_KEYS)


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model.split("/")[-1])


def license_of(model: str) -> str:
    """GGUF 변환본(예: unsloth/Qwen3-4B-Instruct-2507-Q4_K_M)은 원본 모델의 라이선스를 따른다."""
    if model in LICENSES:
        return LICENSES[model]
    base = re.sub(r"-(?:UD-)?(?:I?Q\d\w*|F16|BF16)$", "", model.split("/")[-1], flags=re.IGNORECASE)
    return next((value for key, value in LICENSES.items() if key.split("/")[-1] == base), "확인 필요")


def reason_kind(problem: str) -> str:
    """'사실에 없는 금액: 200,000원' → '사실에 없는 금액'."""
    return re.split(r"[:(]", problem, maxsplit=1)[0].strip()


class RecordingClient:
    """호출마다 서버 사용량(토큰 수, 생성 시간)을 남긴다. 순서는 ai_log와 같다."""

    def __init__(self, inner):
        self.inner = inner
        self.provider, self.model = inner.provider, inner.model
        self.usage: list[dict | None] = []
        self.roles: list[str | None] = []  # FallbackLLM이면 호출마다 답한 쪽(primary·secondary)

    def chat(self, messages: list[dict], max_tokens: int = 200) -> str:
        try:
            return self.inner.chat(messages, max_tokens=max_tokens)
        finally:
            usage = dict(getattr(self.inner, "last_usage", None) or {})
            timings = getattr(self.inner, "last_timings", None) or {}
            usage.update({k: round(timings[k]) for k in TIMING_KEYS if isinstance(timings.get(k), (int, float))})
            self.usage.append(usage or None)
            self.roles.append(getattr(self.inner, "last_role", None))


def digest(messages: list[dict]) -> str:
    """질문(messages)의 지문. Colab 기록과 로컬 재생이 같은 질문인지 맞춰 본다."""
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def draft_of(messages: list[dict]) -> str:
    first_user = next(m["content"] for m in messages if m["role"] == "user")
    match = re.search(r"\[초안\]\n(.*?)\n\n\[요청\]", first_user, flags=re.DOTALL)
    return match.group(1) if match else ""


def narrative_inputs(calc: dict, recommendations: list[dict]) -> dict:
    """write_narrative가 읽는 값만 남긴다. 외부 GPU 실험에 이름·주소·문서를 보내지 않기 위해 쓴다."""
    totals = calc["cashflow"]["totals"]
    return {
        "calc": {
            "effect": {k: calc["effect"][k] for k in ("policy_monthly_won", "adjustment_monthly_won", "monthly_freed_won")},
            "basis": {k: calc["basis"][k] for k in ("inflow_won", "disposable_won")},
            "metrics": [{k: m[k] for k in ("code", "before", "after")} for m in calc["metrics"] if m["code"] == "저축률"],
            "cashflow": {"totals": {k: totals[k] for k in
                                    ("complete", "known_rows", "unknown_rows", "income_before_won", "unexplained_won")}},
        },
        "recommendations": [{"id": r["id"], "kind": r["kind"], "target": r["target"], "action": r["action"],
                             "amount": {k: r["amount"][k] for k in ("before_won", "after_won", "monthly_change_won")}}
                            for r in recommendations],
    }


class ReplayClient:
    """Colab에서 기록한 답을 같은 질문에 그대로 돌려준다. 질문이 달라지면(코드가 바뀌면) 실패로 드러난다."""
    provider = "local-replay"

    def __init__(self, model: str, answers: dict[str, str]):
        self.model, self.answers = model, answers
        self.missing = 0

    def chat(self, messages: list[dict], max_tokens: int = 200) -> str:
        key = digest(messages)
        if key not in self.answers:
            self.missing += 1
            raise LLMUnavailable(f"기록에 없는 질문: {key}")
        return self.answers[key]


def _attempt(entry: dict) -> dict:
    return {"output": entry.get("output") or entry["raw_output"], "raw": entry["raw_output"], "problems": entry["problems"]}


def section_rows(narrative: dict, log: list[dict]) -> list[dict]:
    """문장 묶음마다 초안, 시도한 답과 문제, 최종 문장과 출처."""
    finals = {"summary": narrative["summary"], **{f"coaching:{c['rec_id']}": c for c in narrative["coaching"]}}
    attempts: dict[str, list[dict]] = {}
    for entry in log:
        attempts.setdefault(entry["section"], []).append(entry)
    rows = []
    for section, final in finals.items():
        tries = attempts.get(section, [])
        rows.append({"section": section, "draft": draft_of(tries[0]["messages"]) if tries else final["text"],
                     "final": final["text"], "source": final["source"], "attempts": [_attempt(t) for t in tries]})
    return rows


def rows_from_log(drafts: dict[str, str], log: list[dict]) -> list[dict]:
    """질문 본문 없이 기록된 Colab 결과에서 section_rows와 같은 모양을 만든다."""
    attempts: dict[str, list[dict]] = {}
    for entry in log:
        attempts.setdefault(entry["section"], []).append(entry)
    rows = []
    for section, draft in drafts.items():
        tries = attempts.get(section, [])
        accepted = next((t for t in tries if t["accepted"]), None)
        rows.append({"section": section, "draft": draft,
                     "final": _attempt(accepted)["output"] if accepted else draft,
                     "source": "llm" if accepted else "template", "attempts": [_attempt(t) for t in tries]})
    return rows


def case_stats(rows: list[dict], log: list[dict], usage: list[dict | None], roles: list | None = None) -> dict:
    usage = [u for u in usage if u]
    return {
        "sections": len(rows),
        "llm": sum(r["source"] == "llm" for r in rows),
        "first_pass": sum(bool(r["attempts"]) and not r["attempts"][0]["problems"] for r in rows),
        "calls": len(log),
        "fallback_calls": sum(r == "secondary" for r in roles or []),
        "identical_retry": sum(len(r["attempts"]) > 1 and r["attempts"][0]["raw"] == r["attempts"][1]["raw"]
                               for r in rows),
        "reasons": dict(Counter(reason_kind(p) for e in log for p in e["problems"])),
        "latency_ms": [e["latency_ms"] for e in log if "latency_ms" in e],
        **{key: [u[key] for u in usage if u.get(key) is not None] for key in USAGE_KEYS},
        # 한 번 호출에 쓴 문맥(질문 + 답). 노트북 PC에서 llama.cpp 문맥 크기(-c)를 정할 때 본다
        "input_tokens": [t for t in (u.get("input_tokens", u.get("prompt_tokens")) for u in usage) if t is not None],
        "output_tokens": [t for t in (u.get("output_tokens", u.get("completion_tokens")) for u in usage) if t is not None],
        "context_tokens": [u["prompt_tokens"] + u["completion_tokens"] for u in usage
                           if u.get("prompt_tokens") is not None and u.get("completion_tokens") is not None],
    }


def _p95(values: list[int]) -> int | None:
    return sorted(values)[int(0.95 * (len(values) - 1))] if values else None


def _rate(count: list[int], ms: list[int]) -> float | None:
    return round(sum(count) / (sum(ms) / 1000), 1) if sum(ms) else None


def aggregate(model: str, per_case: dict[str, dict], warmup_seconds: float | None, error: str | None,
              info: dict | None = None) -> dict:
    """info는 Colab 단계의 실행 정보(백엔드, 스레드, 파일 크기, 최대 메모리 등)."""
    total, reasons = Counter(), Counter()
    lists: dict[str, list[int]] = {key: [] for key in LIST_KEYS}
    for stats in per_case.values():
        total.update({key: stats[key] for key in COUNTS})
        reasons.update(stats["reasons"])
        for key in LIST_KEYS:
            lists[key] += stats.get(key, [])
    sections, latency = total["sections"], lists["latency_ms"]
    return {
        "model": model, "license": license_of(model), "error": error, "warmup_seconds": warmup_seconds,
        **{key: total[key] for key in COUNTS},
        "llm_rate": round(total["llm"] / sections, 3) if sections else None,
        "first_pass_rate": round(total["first_pass"] / sections, 3) if sections else None,
        "reasons": dict(reasons.most_common()),
        "latency_ms_median": round(statistics.median(latency)) if latency else None,
        "latency_ms_p95": _p95(latency),
        "tokens_per_second": _rate(lists["completion_tokens"], lists["generation_ms"]),
        "prompt_tokens_per_second": _rate(lists["prompt_n"], lists["prompt_ms"]),
        "generation_tokens_per_second": _rate(lists["predicted_n"], lists["predicted_ms"]),
        "max_context_tokens": max(lists["context_tokens"], default=None),
        "input_tokens_total": sum(lists["input_tokens"]) if lists["input_tokens"] else None,
        "output_tokens_total": sum(lists["output_tokens"]) if lists["output_tokens"] else None,
        **({"run": info} if info else {}),
        "cases": {stem: {k: v for k, v in s.items() if k not in LIST_KEYS} for stem, s in per_case.items()},
    }


def warm_up(client, timeout: float) -> tuple[float | None, str | None]:
    """첫 호출은 서버가 모델을 받아 올리느라 오래 걸린다. 터널은 약 100초에 응답을 끊으므로
    모델이 올라올 때까지 다시 부른다. 인증·모델명 오류(4xx)는 기다리지 않고 실패로 돌려준다."""
    started = time.monotonic()
    error = "제한 시간 안에 응답 없음"
    while time.monotonic() - started < timeout:
        try:
            client.chat([{"role": "user", "content": "안녕하세요"}], max_tokens=4)
            return round(time.monotonic() - started, 1), None
        except LLMUnavailable as exc:
            error = str(exc)
            if getattr(exc, "status", None) in (400, 401, 403, 404):
                break
            time.sleep(min(5.0, timeout / 10))
    return None, error


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run(models: list[str], inputs: list[ReportInput], make_client, out_dir: Path, label: str,
        warmup_timeout: float = 900.0, progress=print) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"label": label, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "cases": [f"{i.case_id}_{i.profile_version}" for i in inputs], "models": []}
    samples: dict[str, dict[str, list[dict]]] = {}
    for model in models:
        client = make_client(model)
        warm, error = warm_up(client, warmup_timeout)
        per_case: dict[str, dict] = {}
        folder = out_dir / model_slug(model)
        if error is None:
            for inp in inputs:
                recorder = RecordingClient(client)
                stem = f"{inp.case_id}_{inp.profile_version}"
                report, log = build_report(inp, recorder, log_file=f"ai_log_{stem}.json")
                _write_reports(folder, report, log)
                rows = section_rows(report["narrative"], log)
                per_case[stem] = case_stats(rows, log, recorder.usage, recorder.roles)
                samples.setdefault(stem, {})[model] = rows
                progress(f"{model_slug(model)} {stem}: LLM {per_case[stem]['llm']}/{per_case[stem]['sections']}")
        else:
            progress(f"{model_slug(model)} 실행 실패: {error}")
        summary["models"].append(aggregate(model, per_case, warm, error))
    _dump(out_dir / "summary.json", summary)
    (out_dir / "summary.md").write_text(summary_markdown(summary), encoding="utf-8")
    (out_dir / "samples.md").write_text(samples_markdown(samples), encoding="utf-8")
    return summary


def _write_reports(folder: Path, report: dict, log: list[dict]) -> None:
    stem = f"{report['case_id']}_{report['profile_version']}"
    folder.mkdir(parents=True, exist_ok=True)
    _dump(folder / f"spending_report_{stem}.json", report)
    (folder / f"spending_report_{stem}.html").write_text(to_html(report), encoding="utf-8")
    _dump(folder / f"ai_log_{stem}.json", log)


def import_colab(results: dict, inputs: list[ReportInput], out_dir: Path, label: str, replay_version: str | None = None,
                 progress=print) -> dict:
    """Colab 결과를 표(summary)와 문장 비교(samples)로 정리한다. replay_version의 답은 같은 질문에 재생해
    모델별 전체 보고서도 만든다. 재생은 로컬 narrative.py가 Colab에서 돌린 그 버전일 때만 맞는다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    replay_version = replay_version or results.get("current_version", "v2")  # 1차 실험(colab_1)은 v2가 현재 코드였다
    groups: dict[tuple[str, str], dict[str, dict]] = {}
    errors: dict[tuple[str, str], str] = {}
    samples: dict[str, dict[str, list[dict]]] = {}
    case_errors: dict[tuple[str, str], list[str]] = {}
    for run in results["runs"]:
        key = (run["model"], run["version"])
        groups.setdefault(key, {})
        if run.get("error"):
            if run["case"] is None:
                errors[key] = run["error"]
            else:
                case_errors.setdefault(key, []).append(f"{run['case']}: {run['error'].strip().splitlines()[-1]}")
            continue
        rows = rows_from_log(results["drafts"][run["case"]], run["log"])
        groups[key][run["case"]] = case_stats(rows, run["log"], run["usage"])
        samples.setdefault(run["case"], {})[f"{run['model']} {run['version']}"] = rows
    summary = {"label": label, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "source": "colab",
               "env": results.get("env"), "cases": list(results["drafts"]), "models": [], "replay": {}}
    for (model, version), per_case in groups.items():
        entry = aggregate(model, per_case, results.get("load_seconds", {}).get(model), errors.get((model, version)),
                          info=results.get("steps", {}).get(model))
        summary["models"].append({**entry, "version": version, "case_errors": case_errors.get((model, version), [])})

    for model in [m for (m, v) in groups if v == replay_version and (m, v) not in errors]:
        answers = {e["sha"]: e["raw_output"] for run in results["runs"] if (run["model"], run["version"]) == (model, replay_version)
                   for e in run.get("log", []) if e["raw_output"] is not None}
        client = ReplayClient(model, answers)
        for inp in inputs:
            report, log = build_report(inp, client, log_file=f"ai_log_{inp.case_id}_{inp.profile_version}.json")
            _write_reports(out_dir / f"{model_slug(model)}_{replay_version}", report, log)
        summary["replay"][model] = {"missing_answers": client.missing}
        progress(f"{model_slug(model)} {replay_version} 재생: 기록에 없는 질문 {client.missing}건")
    _dump(out_dir / "summary.json", summary)
    (out_dir / "summary.md").write_text(summary_markdown(summary), encoding="utf-8")
    (out_dir / "samples.md").write_text(samples_markdown(samples), encoding="utf-8")
    return summary


def _ms(value: int | None) -> str:
    return "-" if value is None else f"{value / 1000:.1f}초"


def _name(m: dict) -> str:
    return m["model"] + (f" [{m['version']}]" if m.get("version") else "")


def _llama_runs(summary: dict) -> list[dict]:
    return [m for m in summary["models"] if (m.get("run") or {}).get("backend") == "llama.cpp"]


def _latency_note(summary: dict) -> str:
    if summary.get("source") != "colab":
        return "- 지연: 이 PC에서 잰 왕복 시간(네트워크 포함). 생성 속도는 서버가 잰 값"
    env = summary.get("env") or {}
    if _llama_runs(summary):
        return (f"- 지연: Colab CPU({env.get('cpu') or '?'})에서 llama.cpp로 잰 호출 한 번의 시간(프롬프트 처리 포함). "
                "GPU는 쓰지 않았다. 생성 속도는 서버가 잰 값")
    return "- 지연: Colab GPU에서 잰 호출 한 번의 생성 시간" + (f"({env['gpu']})" if env.get("gpu") else "")


def _tps(value: float | None) -> str:
    return "-" if value is None else f"{value} 토큰/초"


def _llama_table(summary: dict) -> list[str]:
    runs = _llama_runs(summary)
    if not runs:
        return []
    lines = ["", "## llama.cpp 실행 정보", "",
             "프롬프트 처리와 생성 속도는 서버가 잰 값이다. 최대 메모리는 서버 프로세스가 가장 많이 잡은 메모리(모델 파일 포함), "
             "최대 사용 문맥은 호출 한 번의 질문과 답을 합친 토큰 수다.", "",
             "| 모델 | 파일 | 스레드 | 문맥(-c) | 최대 메모리 | 프롬프트 처리 | 생성 | 최대 사용 문맥 |",
             "|---|---|---|---|---|---|---|---|"]
    for m in runs:
        run = m["run"]
        memory = "-" if run.get("peak_rss_mb") is None else f"{run['peak_rss_mb'] / 1024:.1f}GB"
        lines.append(f"| {_name(m)} | {run['size_bytes'] / 1e9:.2f}GB | {run.get('threads')} | {run.get('ctx')} | {memory} "
                     f"| {_tps(m['prompt_tokens_per_second'])} | {_tps(m['generation_tokens_per_second'])} "
                     f"| {m['max_context_tokens'] or '-'} |")
    return lines


def summary_markdown(summary: dict) -> str:
    lines = [f"# LLM 문장 실험: {summary['label']}", "",
             f"- 실행: {summary['generated_at']}",
             f"- 고객: {', '.join(summary['cases'])}",
             "- 채택: 검증을 통과해 보고서에 쓰인 LLM 문장 수. 나머지는 템플릿 문장으로 되돌렸다.",
             _latency_note(summary),
             "",
             "| 모델 | 라이선스 | 채택 | 1회차 통과 | 같은 답 재시도 | 지연 중앙값 | 지연 p95 | 생성 속도 | 거절 사유 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for m in summary["models"]:
        if m["error"]:
            lines.append(f"| {_name(m)} | {m['license']} | 실행 실패: {m['error'][:80]} | | | | | | |")
            continue
        reasons = ", ".join(f"{k} {v}" for k, v in m["reasons"].items()) or "없음"
        speed = _tps(m.get("generation_tokens_per_second") or m["tokens_per_second"])
        lines.append(f"| {_name(m)} | {m['license']} | {m['llm']}/{m['sections']} ({m['llm_rate']:.0%}) "
                     f"| {m['first_pass']}/{m['sections']} | {m['identical_retry']} | {_ms(m['latency_ms_median'])} "
                     f"| {_ms(m['latency_ms_p95'])} | {speed} | {reasons} |")
    lines += _llama_table(summary)
    fallback = [m for m in summary["models"] if m.get("fallback_calls")]
    if fallback:
        lines += ["", "## 대체 모델 호출", "", "| 모델 | 대체 호출 | 전체 호출 |", "|---|---|---|"]
        lines += [f"| {_name(m)} | {m['fallback_calls']} | {m['calls']} |" for m in fallback]
    tokens = [m for m in summary["models"] if m.get("input_tokens_total") is not None or m.get("output_tokens_total") is not None]
    if tokens:
        lines += ["", "## 토큰 합계", "", "| 모델 | 입력 토큰 | 출력 토큰 |", "|---|---|---|"]
        lines += [f"| {_name(m)} | {m.get('input_tokens_total') or 0:,} | {m.get('output_tokens_total') or 0:,} |" for m in tokens]
    lines += ["", "## 고객별 채택", "", "| 모델 | " + " | ".join(summary["cases"]) + " |",
              "|---|" + "---|" * len(summary["cases"])]
    for m in summary["models"]:
        cells = [f"{m['cases'][c]['llm']}/{m['cases'][c]['sections']}" if c in m["cases"] else "-"
                 for c in summary["cases"]]
        lines.append(f"| {_name(m).split('/')[-1]} | " + " | ".join(cells) + " |")
    failed = [f"- {_name(m)} {e}" for m in summary["models"] for e in m.get("case_errors", [])]
    if failed:
        lines += ["", "## 실행 중 오류", "", *failed]
    if summary.get("replay"):
        lines += ["", "## 로컬 재생", "",
                  "Colab 답을 지금 코드의 같은 질문에 다시 넣어 전체 보고서를 만들었다. 기록에 없는 질문이 생기면 "
                  "지금 코드가 그 답을 거절하고 다시 물은 것이므로, 그 모델의 재생 보고서는 일부가 템플릿이다.", ""]
        lines += [f"- {model}: 기록에 없는 질문 {info['missing_answers']}건" for model, info in summary["replay"].items()]
    return "\n".join(lines) + "\n"


def load_colab_results(path: Path) -> dict:
    """results.json, 또는 마지막 셀에 RESULTS_B64가 출력된 채 저장된 노트북(.ipynb)을 읽는다."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".b64":  # 마지막 셀 출력만 옮겨 온 경우
        blob = text.split("RESULTS_B64:", 1)[-1].strip()
        return json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))
    if path.suffix != ".ipynb":
        return json.loads(text)
    for cell in json.loads(text)["cells"]:
        for output in cell.get("outputs", []):
            body = output.get("text", "")
            body = "".join(body) if isinstance(body, list) else body
            if "RESULTS_B64:" in body:
                blob = body.split("RESULTS_B64:", 1)[1].split()[0]
                return json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))
    raise ValueError("노트북에 RESULTS_B64 출력이 없습니다")


def _one_line(text: str | None) -> str:
    return re.sub(r"\s*\n\s*", " / ", text or "").strip()


def samples_markdown(samples: dict[str, dict[str, list[dict]]]) -> str:
    lines = ["# 문장 비교", "", "모델별 최종 문장. 템플릿으로 되돌린 경우 마지막 시도와 거절 사유를 적는다.", ""]
    for stem, by_model in samples.items():
        lines += [f"## {stem}", ""]
        order = [r["section"] for r in next(iter(by_model.values()))]
        indexed = {model: {r["section"]: r for r in rows} for model, rows in by_model.items()}
        for section in order:
            first = next(rows[section] for rows in indexed.values() if section in rows)
            lines += [f"### {section}", "", f"- 초안: {_one_line(first['draft'])}"]
            for model, rows in indexed.items():
                row = rows.get(section)
                if row is None:
                    continue
                if row["source"] == "llm":
                    note = f" (1회차 거절: {'; '.join(row['attempts'][0]['problems'])})" if len(row["attempts"]) > 1 else ""
                    lines.append(f"- {model_slug(model)}{note}: {_one_line(row['final'])}")
                elif row["attempts"]:
                    last = row["attempts"][-1]
                    lines.append(f"- {model_slug(model)} (거절: {'; '.join(last['problems'])}): {_one_line(last['output'])}")
                else:
                    lines.append(f"- {model_slug(model)} (호출 없음)")
            lines.append("")
    return "\n".join(lines)


def parse_case(text: str) -> tuple[str, str | None]:
    case_id, _, version = text.partition(":")
    return case_id, version or None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=[DEFAULT_MODEL])
    parser.add_argument("--cases", nargs="+", default=DEFAULT_CASES, help="case_id 또는 case_id:version")
    parser.add_argument("--label", default=datetime.now().strftime("%Y%m%d_%H%M"))
    parser.add_argument("--provider", choices=["server", "anthropic"], default="server",
                        help="server: OpenAI 호환(/chat/completions), anthropic: Messages 형식(LLM_MESSAGES_PATH, 기본 /v1/messages)")
    parser.add_argument("--base-url", default=os.getenv("LLM_BASE_URL"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("LLM_TIMEOUT") or 180))
    parser.add_argument("--warmup-timeout", type=float, default=900)
    parser.add_argument("--colab-results", type=Path, help="colab/llm_eval.ipynb가 만든 results.json. 서버 대신 이 기록을 쓴다")
    parser.add_argument("--db", default=Path("data/customer_profiles.db"), type=Path)
    parser.add_argument("--out", default=Path("output/llm_eval"), type=Path)
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    progress = lambda m: print(m, flush=True)  # noqa: E731
    if args.colab_results:
        results = load_colab_results(args.colab_results)
        stems = list(results["drafts"])
        inputs = [load_input(conn, *stem.rsplit("_", 1)) for stem in stems]
        summary = import_colab(results, inputs, args.out / args.label, args.label, progress=progress)
    else:
        if not args.base_url:
            parser.error("--base-url 또는 LLM_BASE_URL이 필요합니다")
        api_key = os.getenv("LLM_API_KEY")
        inputs = [load_input(conn, case_id, version) for case_id, version in map(parse_case, args.cases)]
        path = os.getenv("LLM_MESSAGES_PATH") or DEFAULT_MESSAGES_PATH

        def make_client(model):
            if args.provider == "anthropic":
                primary = AnthropicLLM(model, args.base_url, timeout=args.timeout, api_key=api_key, messages_path=path)
            else:
                primary = ServerLLM(model, args.base_url, timeout=args.timeout, api_key=api_key)
            return with_fallback(primary)  # LLM_FALLBACK_PROVIDER가 있으면 보조 모델로 대체
        summary = run(args.models, inputs, make_client,
                      args.out / args.label, args.label, args.warmup_timeout, progress=progress)
    print(summary_markdown(summary))


if __name__ == "__main__":
    main()
