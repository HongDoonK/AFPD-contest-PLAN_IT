"""Colab에서 보고서 문장 생성 실험을 돌린다(colab/llm_eval.ipynb가 이 파일을 담아 실행한다).

plan.json의 단계(모델)·버전마다 inputs.json의 고객별 계산 결과(이름·주소 없음)로 write_narrative를 실행하고
results.json과 압축본 results.b64를 남긴다. 질문 본문은 빼고 지문(sha)만 남긴다. 로컬에서 같은 코드로
같은 질문을 만들면 지문이 같아서, 기록된 답을 재생해 전체 보고서를 다시 만들 수 있다.

plan.json: {"versions": {"v4": "spending_report.narrative", ...},
            "steps": [{"model": ..., "versions": [...]},
                      {"model": ..., "versions": [...], "gguf": {"repo": ..., "file": ..., "sha256": ...},
                       "threads": 4, "ctx": 4096}]}

gguf가 없는 단계는 transformers로 GPU에서 돌린다. gguf 단계는 llama.cpp 서버(CPU)를 이 런타임 안(127.0.0.1)에서만
띄우고 ServerLLM으로 부른다. 노트북 PC에서 LLM_PROVIDER=server로 쓰는 경로와 같다.
단계가 끝날 때마다 결과를 저장하므로 중간에 멈춰도 끝난 단계는 남는다.
"""
import base64
import gc
import hashlib
import importlib
import json
import os
import re
import subprocess
import time
import traceback
import urllib.request
import zlib

CURRENT_MODULE = "spending_report.narrative"
LLAMA_SERVER = "/content/llama.cpp/build/bin/llama-server"
PORT = 8090


def digest(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def draft_of(messages):
    first_user = next(m["content"] for m in messages if m["role"] == "user")
    match = re.search(r"\[초안\]\n(.*?)\n\n\[요청\]", first_user, flags=re.DOTALL)
    return match.group(1) if match else ""


class Recorder:
    """호출마다 걸린 시간과 토큰 수를 남긴다. llama.cpp 서버면 서버가 잰 프롬프트 처리·생성 시간도 남긴다."""

    def __init__(self, inner):
        self.inner, self.provider, self.model = inner, inner.provider, inner.model
        self.usage = []

    def chat(self, messages, max_tokens=200):
        started = time.perf_counter()
        text = self.inner.chat(messages, max_tokens=max_tokens)
        item = {"generation_ms": round((time.perf_counter() - started) * 1000)}
        usage = getattr(self.inner, "last_usage", None)
        tokenizer = getattr(self.inner, "_tokenizer", None)
        if usage:
            item.update(prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
        else:
            item["completion_tokens"] = len(tokenizer(text)["input_ids"]) if tokenizer else None
        timings = getattr(self.inner, "last_timings", None) or {}
        for key in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms"):
            if isinstance(timings.get(key), (int, float)):
                item[key] = round(timings[key])
        self.usage.append(item)
        return text


def compact(log, drafts):
    """ai_log에서 질문 본문을 빼고 지문만 남긴다. 초안은 고객·문장 묶음별로 한 번만 drafts에 둔다."""
    out = []
    for entry in log:
        if entry["attempt"] == 1:
            drafts.setdefault(entry["section"], draft_of(entry["messages"]))
        item = {k: entry.get(k) for k in ("section", "attempt", "raw_output", "problems", "accepted", "latency_ms")}
        if entry.get("output") not in (None, entry["raw_output"]):
            item["output"] = entry["output"]
        item["sha"] = digest(entry["messages"])
        out.append(item)
    return out


def cpu_name():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            return next((line.split(":", 1)[1].strip() for line in f if line.startswith("model name")), None)
    except OSError:
        return None


def environment():
    env = {"cpu": cpu_name(), "cpu_count": os.cpu_count()}
    try:
        import torch
        import transformers
    except ModuleNotFoundError:
        return env
    env.update(torch=torch.__version__, transformers=transformers.__version__)
    if torch.cuda.is_available():
        env["gpu"] = torch.cuda.get_device_name(0)
    return env


def llama_version():
    try:
        out = subprocess.run([LLAMA_SERVER, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"실행 불가: {exc!r}"[:200]
    return (out.stdout + out.stderr).strip()[-200:]


def sha256_of(path):
    digest_ = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 24), b""):
            digest_.update(block)
    return digest_.hexdigest()


def server_command(path, threads, ctx, port=PORT):
    """llama.cpp 서버 실행 명령. 이 런타임 안(127.0.0.1)에서만 듣고 GPU는 쓰지 않는다(CPU 전용 빌드, -ngl 0).
    -c를 주지 않으면 모델 최대 문맥(26만 토큰)만큼 메모리를 잡으므로 꼭 준다."""
    return [LLAMA_SERVER, "-m", path, "--host", "127.0.0.1", "--port", str(port), "-t", str(threads),
            "-c", str(ctx), "-np", "1", "-ngl", "0", "--jinja"]


def wait_ready(proc, port=PORT, timeout=600):
    """서버가 모델을 다 올려 /health가 200을 줄 때까지 기다린다."""
    started = time.time()
    while time.time() - started < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server가 멈춤(코드 {proc.returncode})")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                if resp.status == 200:
                    return round(time.time() - started, 1)
        except OSError:  # 올리는 중(503)이거나 아직 포트가 안 열림
            pass
        time.sleep(2)
    raise RuntimeError(f"llama-server가 {timeout}초 안에 준비되지 않음")


def peak_rss_mb(pid):
    """프로세스의 최대 상주 메모리(MB). mmap으로 올린 모델 파일도 포함된다."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return round(int(line.split()[1]) / 1024)
    except OSError:
        pass
    return None


def open_local(step):
    import torch
    import transformers

    from spending_report.llm import LocalLLM

    os.environ["LLM_ALLOW_DOWNLOAD"] = "1"
    transformers.logging.set_verbosity_error()
    client = LocalLLM(step["model"])
    client._load()
    info = {"backend": "transformers", "dtype": str(next(client._model.parameters()).dtype)}

    def close():
        gc.collect()
        torch.cuda.empty_cache()

    return client, close, info


def open_gguf(step):
    from huggingface_hub import hf_hub_download

    from spending_report.llm import ServerLLM

    spec = step["gguf"]
    started = time.time()
    path = hf_hub_download(spec["repo"], spec["file"], local_dir="gguf")
    info = {"backend": "llama.cpp", "repo": spec["repo"], "file": spec["file"], "size_bytes": os.path.getsize(path),
            "download_seconds": round(time.time() - started, 1), "sha256": sha256_of(path)}
    if spec.get("sha256") and info["sha256"] != spec["sha256"]:
        raise RuntimeError(f"{spec['file']} 지문이 기록과 다름: {info['sha256']}")
    info["threads"] = step.get("threads") or max(1, (os.cpu_count() or 2) // 2)
    info["ctx"] = step.get("ctx", 4096)
    log_path = f"llama_server_{step['model'].split('/')[-1]}.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(server_command(path, info["threads"], info["ctx"]), stdout=log, stderr=subprocess.STDOUT)
    try:
        info["server_ready_seconds"] = wait_ready(proc)
    except Exception as exc:
        proc.kill()
        log.close()
        tail = open(log_path, encoding="utf-8", errors="replace").read()[-300:]
        raise RuntimeError(f"{exc}: {tail}") from exc
    client = ServerLLM(step["model"], f"http://127.0.0.1:{PORT}/v1", timeout=900, retries=0)

    def close():
        info["peak_rss_mb"] = peak_rss_mb(proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    return client, close, info


def save(results):
    raw = json.dumps(results, ensure_ascii=False).encode("utf-8")
    with open("results.json", "wb") as f:
        f.write(raw)
    with open("results.b64", "w") as f:
        f.write(base64.b64encode(zlib.compress(raw, 9)).decode())
    return len(raw)


def main():
    cases = json.load(open("inputs.json", encoding="utf-8"))
    plan = json.load(open("plan.json", encoding="utf-8"))
    versions = plan["versions"]
    results = {"env": environment(),
               "current_version": next(name for name, module in versions.items() if module == CURRENT_MODULE),
               "drafts": {}, "load_seconds": {}, "steps": {}, "runs": []}
    if any("gguf" in step for step in plan["steps"]):
        results["env"]["llama_cpp"] = llama_version()
    print(results["env"], flush=True)
    for step in plan["steps"]:
        model = step["model"]
        started = time.time()
        try:
            client, close, info = (open_gguf if "gguf" in step else open_local)(step)
        except Exception as exc:  # 모델 하나가 안 올라와도 나머지는 돈다
            for version in step["versions"]:
                results["runs"].append({"model": model, "version": version, "case": None, "error": repr(exc)[:600]})
            print(model, "준비 실패", repr(exc)[:600], flush=True)
            save(results)
            continue
        results["load_seconds"][model] = round(time.time() - started, 1)
        results["steps"][model] = info
        print(model, "준비", results["load_seconds"][model], "초", info, flush=True)
        try:
            for version in step["versions"]:
                module = importlib.import_module(versions[version])
                for case in cases:
                    recorder = Recorder(client)
                    started = time.time()
                    try:
                        _, log = module.write_narrative(case["calc"], case["recommendations"], recorder)
                        run = {"log": compact(log, results["drafts"].setdefault(case["stem"], {})),
                               "usage": recorder.usage}
                    except Exception:
                        run = {"error": traceback.format_exc()[-600:]}
                    run.update(model=model, version=version, case=case["stem"], seconds=round(time.time() - started, 1))
                    results["runs"].append(run)
                    adopted = len({e["section"] for e in run.get("log", []) if e["accepted"]})
                    print(f"{model.split('/')[-1]} {version} {case['stem']}: 채택 {adopted}/"
                          f"{len(results['drafts'].get(case['stem'], {}))}, {run['seconds']}초", flush=True)
        finally:
            # recorder.inner도 모델을 붙잡고 있다. 둘 다 놓아야 다음 모델(14B 등)이 40GB GPU에 올라간다
            client = recorder = None
            close()
        save(results)
    print("results.json", save(results), "bytes", flush=True)


if __name__ == "__main__":
    main()
