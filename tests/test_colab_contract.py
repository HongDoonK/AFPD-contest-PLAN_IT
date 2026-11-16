"""Colab 실험(colab/run_bench.py)과 로컬 정리(llm_eval --colab-results)가 같은 질문을 만드는지 시험한다."""
import base64
import importlib.util
import json
import re
import zlib
from pathlib import Path

import pytest
from conftest import offset

from spending_report import engine
from spending_report.llm import LLMUnavailable
from spending_report.llm_eval import import_colab, load_colab_results, narrative_inputs
from spending_report.narrative import write_narrative

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load("run_bench", ROOT / "colab" / "run_bench.py")


class Scripted:
    """초안을 그대로 돌려주되, 첫 습관 문장은 없는 금액으로 한 번 거절된 뒤 고쳐 쓴다."""
    provider, model = "local", "org/model"

    def __init__(self):
        self.calls = []

    def chat(self, messages, max_tokens=200):
        self.calls.append(messages)
        if len(messages) == 2 and len(self.calls) == 2:
            return "하루 커피값 4,500원을 아껴 보세요."
        return messages[1]["content"].split("[초안]\n", 1)[1].split("\n\n[요청]", 1)[0]


def _case(make_input):
    inp = make_input("kim_doyun", [("고정지출", "교통비", [offset("K-패스(K패스)", 24_000)])])
    analysis = engine.analyze_rows(inp)
    return inp, engine.calculate(inp, analysis), analysis.recommendations


def test_trimmed_inputs_ask_the_same_questions(make_input):
    _, calc, recs = _case(make_input)
    full, trimmed = Scripted(), Scripted()
    write_narrative(calc, recs, full)
    payload = json.loads(json.dumps(narrative_inputs(calc, recs), ensure_ascii=False))
    write_narrative(payload["calc"], payload["recommendations"], trimmed)
    assert full.calls == trimmed.calls and len(full.calls) > 2
    assert "김도윤" not in json.dumps(payload, ensure_ascii=False)


def test_colab_results_replay_into_full_reports(make_input, tmp_path):
    inp, calc, recs = _case(make_input)
    payload = narrative_inputs(calc, recs)
    stem = f"{inp.case_id}_{inp.profile_version}"
    recorder = bench.Recorder(Scripted())
    _, log = write_narrative(payload["calc"], payload["recommendations"], recorder)
    results = {"env": {"gpu": "test"}, "drafts": {}, "load_seconds": {"org/model": 1.0}, "runs": []}
    results["runs"].append({"model": "org/model", "version": "v2", "case": stem, "seconds": 1.0,
                            "log": bench.compact(log, results["drafts"].setdefault(stem, {})), "usage": recorder.usage})
    summary = import_colab(results, [inp], tmp_path, "t", progress=lambda m: None)
    entry = summary["models"][0]
    assert summary["replay"]["org/model"]["missing_answers"] == 0
    assert entry["llm"] == entry["sections"] and entry["first_pass"] == entry["sections"] - 1
    report = json.loads((tmp_path / "model_v2" / f"spending_report_{stem}.json").read_text(encoding="utf-8"))
    first = report["narrative"]["coaching"][0]
    assert first["source"] == "llm" and first["text"].endswith("줄이는 안입니다.")
    assert report["ai_usage"]["provider"] == "local-replay"
    assert "사실에 없는 금액: 4,500원" in (tmp_path / "samples.md").read_text(encoding="utf-8")


class LlamaServerLike:
    """llama.cpp 서버처럼 토큰 수와 서버가 잰 시간을 남기는 클라이언트."""
    provider, model = "server", "unsloth/Qwen3-4B-Instruct-2507-Q4_K_M"

    def __init__(self):
        self.inner = Scripted()
        self.last_usage = self.last_timings = None

    def chat(self, messages, max_tokens=200):
        text = self.inner.chat(messages, max_tokens)
        self.last_usage = {"prompt_tokens": 600, "completion_tokens": 40, "total_tokens": 640}
        self.last_timings = {"prompt_n": 300, "prompt_ms": 5000.4, "predicted_n": 40, "predicted_ms": 4000.6}
        return text


def test_llama_server_runs_report_cpu_speed_and_memory(make_input, tmp_path):
    inp, calc, recs = _case(make_input)
    payload = narrative_inputs(calc, recs)
    stem = f"{inp.case_id}_{inp.profile_version}"
    model = LlamaServerLike.model
    recorder = bench.Recorder(LlamaServerLike())
    _, log = write_narrative(payload["calc"], payload["recommendations"], recorder)
    first = {k: v for k, v in recorder.usage[0].items() if k != "generation_ms"}
    assert first == {"prompt_tokens": 600, "completion_tokens": 40,
                     "prompt_n": 300, "prompt_ms": 5000, "predicted_n": 40, "predicted_ms": 4001}
    results = {"env": {"cpu": "Test CPU", "gpu": "A100"}, "current_version": "v4", "drafts": {}, "runs": [],
               "load_seconds": {model: 30.0},
               "steps": {model: {"backend": "llama.cpp", "file": "x.gguf", "size_bytes": 2_497_281_120,
                                 "threads": 4, "ctx": 4096, "peak_rss_mb": 3200}}}
    results["runs"].append({"model": model, "version": "v4", "case": stem, "seconds": 1.0,
                            "log": bench.compact(log, results["drafts"].setdefault(stem, {})), "usage": recorder.usage})
    summary = import_colab(results, [inp], tmp_path, "t", progress=lambda m: None)
    entry = summary["models"][0]
    assert entry["license"] == "Apache-2.0" and entry["run"]["threads"] == 4
    assert entry["prompt_tokens_per_second"] == 60.0 and entry["generation_tokens_per_second"] == 10.0
    assert entry["max_context_tokens"] == 640
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "llama.cpp 실행 정보" in text and "Test CPU" in text and "GPU는 쓰지 않았다" in text and "3.1GB" in text
    assert summary["replay"][model]["missing_answers"] == 0
    assert (tmp_path / "Qwen3-4B-Instruct-2507-Q4_K_M_v4" / f"spending_report_{stem}.json").exists()


def test_llama_server_listens_only_inside_the_runtime():
    command = bench.server_command("model.gguf", 4, 4096)
    assert command[command.index("--host") + 1] == "127.0.0.1" and "0.0.0.0" not in command
    assert command[command.index("-c") + 1] == "4096" and command[command.index("-t") + 1] == "4"
    assert command[command.index("-ngl") + 1] == "0"


def test_replay_reports_questions_it_never_saw(make_input, tmp_path):
    inp, _, _ = _case(make_input)
    stem = f"{inp.case_id}_{inp.profile_version}"
    results = {"drafts": {stem: {"summary": "초안"}}, "runs": [
        {"model": "org/model", "version": "v2", "case": stem, "usage": [],
         "log": [{"section": "summary", "attempt": 1, "raw_output": "다른 답입니다.", "problems": [], "accepted": True,
                  "latency_ms": 5, "sha": "0000000000000000"}]}]}
    summary = import_colab(results, [inp], tmp_path, "t", progress=lambda m: None)
    assert summary["replay"]["org/model"]["missing_answers"] == 1  # 코드가 달라지면 재생이 틀어진 것을 드러낸다


def test_results_are_read_back_from_a_saved_notebook(tmp_path):
    results = {"drafts": {}, "runs": [], "env": {"gpu": "A100"}}
    blob = base64.b64encode(zlib.compress(json.dumps(results).encode())).decode()
    notebook = {"cells": [{"cell_type": "code", "outputs": [{"output_type": "stream", "name": "stdout",
                                                             "text": ["RESULTS_B64:" + blob + "\n"]}]}]}
    path = tmp_path / "saved.ipynb"
    path.write_text(json.dumps(notebook), encoding="utf-8")
    assert load_colab_results(path) == results


@pytest.mark.skipif(not (ROOT / "data" / "customer_profiles.db").exists(), reason="고객 DB 없음")
def test_notebook_carries_exact_sources_and_no_personal_details(tmp_path):
    builder = _load("build_notebook", ROOT / "colab" / "build_notebook.py")
    baseline = tmp_path / "narrative_old.py"
    baseline.write_text("# 이전 버전\n", encoding="utf-8")
    notebook = builder.build(ROOT / "data" / "customer_profiles.db", "v3", baseline, "v2")
    cells = {"".join(c["source"]).split("\n", 1)[0]: "".join(c["source"]) for c in notebook["cells"]}
    written = cells["%%writefile spending_report/narrative.py"].split("\n", 1)[1]
    assert written == builder.normalize((ROOT / "spending_report" / "narrative.py").read_text(encoding="utf-8"))
    assert cells["%%writefile spending_report/narrative_base.py"].endswith("# 이전 버전")
    inputs = json.loads(cells["%%writefile inputs.json"].split("\n", 1)[1])
    assert [c["stem"] for c in inputs][:2] == ["kim_doyun_v2", "lee_seojun_v2"] and len(inputs) == 6
    plan = json.loads(cells["%%writefile plan.json"].split("\n", 1)[1])
    assert plan["versions"] == {"v3": "spending_report.narrative", "v2": "spending_report.narrative_base"}
    assert plan["steps"][0]["versions"] == ["v2", "v3"] and plan["steps"][-1]["versions"] == ["v3"]
    text = json.dumps(notebook, ensure_ascii=False)
    for private in ("김도윤", "이서준", "김민지", "박준호", "이서연", "김현우", "성남", "분당"):
        assert private not in text
    assert notebook["metadata"]["colab"]["gpuType"] == "A100"
    assert "cloudflared" not in text and "drive.mount" not in text


@pytest.mark.skipif(not (ROOT / "data" / "customer_profiles.db").exists(), reason="고객 DB 없음")
def test_gguf_notebook_pins_model_files_and_stays_local():
    builder = _load("build_notebook", ROOT / "colab" / "build_notebook.py")
    notebook = builder.build(ROOT / "data" / "customer_profiles.db", "v4", gguf=True)
    cells = {"".join(c["source"]).split("\n", 1)[0]: "".join(c["source"]) for c in notebook["cells"]}
    plan = json.loads(cells["%%writefile plan.json"].split("\n", 1)[1])
    assert plan["versions"] == {"v4": "spending_report.narrative"}
    assert [s["model"].rsplit("-", 1)[-1] for s in plan["steps"]] == list(builder.GGUF_QUANTS)
    for step in plan["steps"]:
        assert re.fullmatch(r"[0-9a-f]{64}", step["gguf"]["sha256"]) and step["threads"] == 4
        assert step["gguf"]["file"] == step["model"].split("/")[-1] + ".gguf"
    assert f"--branch {builder.LLAMA_CPP_TAG}" in cells["%%bash"] and "-DGGML_CUDA=OFF" in cells["%%bash"]
    written = cells["%%writefile run_bench.py"].split("\n", 1)[1]
    assert written == builder.normalize((ROOT / "colab" / "run_bench.py").read_text(encoding="utf-8"))
    text = json.dumps(notebook, ensure_ascii=False)
    for word in ("cloudflared", "ngrok", "drive.mount", "0.0.0.0", "김도윤", "성남"):
        assert word not in text


def test_unavailable_model_does_not_stop_the_bench():
    assert issubclass(LLMUnavailable, RuntimeError)
