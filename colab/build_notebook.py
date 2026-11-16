"""colab/llm_eval.ipynb를 만든다. 실제 소스 파일과 고객별 문장 입력(이름·주소·문서 없음)을 셀에 담는다.

    python -X utf8 colab/build_notebook.py --current-name v3 [--baseline <이전 narrative.py> --baseline-name v2]
    python -X utf8 colab/build_notebook.py --current-name v4 --gguf   # 노트북 PC용 양자화 모델(CPU, llama.cpp)

노트북은 외부로 열리는 서버나 드라이브 연결 없이 Colab 런타임 안에서만 돈다. 결과는 마지막 셀에
RESULTS_B64로 출력되고 노트북과 함께 저장되며, 로컬에서 llm_eval --colab-results로 정리한다.
온도 0이라 같은 코드·모델의 답은 다시 돌려도 같으므로, 이미 돌린 버전은 다시 넣지 않아도 된다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spending_report import engine  # noqa: E402
from spending_report.inputs import load_input  # noqa: E402
from spending_report.llm_eval import DEFAULT_CASES, narrative_inputs  # noqa: E402

# 1차(colab_1)에서 1.5B·1.7B는 머리말·엉뚱한 습관이 잦아 뺐다. Kanana는 transformers 5.x에서 설정 검증에 걸려 로드되지 않는다
MODELS = [
    "Qwen/Qwen2.5-3B-Instruct",
    "Qwen/Qwen3-4B-Instruct-2507",
    "Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2.5-14B-Instruct",
]
BASELINE_MODELS = MODELS[:2]  # 이전 버전과 견줄 모델

# 노트북 PC(CPU)용 양자화 후보. Qwen 공식 GGUF가 없어 가장 많이 쓰이는 unsloth 변환본(Apache-2.0)을 쓴다.
# 값은 Hugging Face LFS sha256(2026-11-10 확인). 파일이 바뀌면 그 단계가 실패로 드러난다
GGUF_REPO = "unsloth/Qwen3-4B-Instruct-2507-GGUF"
GGUF_QUANTS = {  # 노트북 후보 Q4_K_M, 기준 Q8_0, 더 작은 Q3_K_M, 중간 Q5_K_M 순서로 돈다
    "Q4_K_M": "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
    "Q8_0": "391c1e410fd9f4cf2de2b510273b56a84c19ce18f4fa3bfb3774031dac4ef068",
    "Q3_K_M": "9c6e0763577125a994a9bea0bbd7a737ac4498b8a6a4e0f788727553af1806c9",
    "Q5_K_M": "5bde5e9d883622acb02bf77fe7dcbc56a8b9a9ad4be78a72ca23a532658b4ecb",
}
GGUF_THREADS = 4  # 노트북 PC(i5-1135G7)의 물리 코어 수
GGUF_CTX = 4096
LLAMA_CPP_TAG = "v0.9.0"

INTRO = """# AFPK 소비습관 보고서: LLM 문장 실험

런타임 > 모두 실행. 고객 6명의 계산 결과(이름·주소·문서 없음)로 보고서 문장을 모델별로 만들고 검증한다. 외부로 열리는 서버나 드라이브 연결 없이 이 런타임 안에서만 돈다.

- 문장 생성 코드(narrative.py)의 버전과 모델은 plan.json 셀에 있다.
- 결과는 마지막 셀에 `RESULTS_B64:`로 출력되어 노트북과 함께 저장된다. 로컬에서 `python -X utf8 -m spending_report.llm_eval --colab-results <저장된 노트북>`으로 정리한다.
- 끝나면 런타임 > 런타임 연결 해제 및 삭제"""

SETUP = """!nvidia-smi --query-gpu=name,memory.total --format=csv
!mkdir -p spending_report policy_enrich && touch spending_report/__init__.py policy_enrich/__init__.py
import importlib.metadata as md
from packaging.version import Version
if Version(md.version('transformers')) < Version('4.56'):  # LocalLLM의 dtype 인자, Qwen3 지원
    !pip install -q -U 'transformers>=4.56'
print({p: md.version(p) for p in ('torch', 'transformers')})"""

INTRO_GGUF = f"""# AFPK 소비습관 보고서: 노트북용 양자화(GGUF) 문장 실험

런타임 > 모두 실행. 고객 6명의 계산 결과(이름·주소·문서 없음)로 보고서 문장을 만들고 검증한다. GPU는 쓰지 않는다. llama.cpp를 CPU 전용으로 빌드해 노트북 PC(4코어)처럼 {GGUF_THREADS}스레드로 돌린다. A100 런타임은 CPU 코어 수 때문에 고른다.

- 모델 파일: {GGUF_REPO}의 {', '.join(GGUF_QUANTS)}. 받은 뒤 sha256을 확인한다.
- llama.cpp 서버는 이 런타임 안(127.0.0.1)에서만 듣는다. 외부로 열리는 서버, 터널, 드라이브 연결은 쓰지 않는다.
- 결과는 단계마다 저장되고 마지막 셀에 `RESULTS_B64:`로 출력된다.
- 끝나면 런타임 > 런타임 연결 해제 및 삭제"""

SETUP_GGUF = f"""%%bash
# llama.cpp를 CPU 전용으로 빌드한다. 서버는 run_bench.py가 이 런타임 안(127.0.0.1)에서만 띄운다
set -e
nproc; grep -m1 'model name' /proc/cpuinfo; free -g | head -2
mkdir -p spending_report policy_enrich && touch spending_report/__init__.py policy_enrich/__init__.py
[ -d /content/llama.cpp ] || git clone -q --depth 1 --branch {LLAMA_CPP_TAG} https://github.com/ggml-org/llama.cpp /content/llama.cpp
cmake -S /content/llama.cpp -B /content/llama.cpp/build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=OFF \\
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF > /content/cmake.log 2>&1 || {{ tail -20 /content/cmake.log; exit 1; }}
cmake --build /content/llama.cpp/build --config Release -t llama-server -j "$(nproc)" > /content/build.log 2>&1 || {{ tail -30 /content/build.log; exit 1; }}
/content/llama.cpp/build/bin/llama-server --version 2>&1 | tail -2"""

VERIFY = """import hashlib, pathlib
expected = {expected}
for path, want in expected.items():
    text = pathlib.Path(path).read_text(encoding='utf-8').rstrip('\\n')
    got = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
    print('OK  ' if got == want else 'DIFF', path)"""


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def short_sha(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:16]


def cell(kind: str, source: str) -> dict:
    parts = normalize(source).split("\n")
    lines = [p + "\n" for p in parts[:-1]] + [parts[-1]]
    if kind == "markdown":
        return {"cell_type": "markdown", "metadata": {}, "source": lines}
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": lines}


def inputs_json(cases: list[dict]) -> str:
    """한 줄이 너무 길지 않게 권고안 하나를 한 줄에 둔다."""
    blocks = []
    for case in cases:
        recs = ",\n  ".join(json.dumps(r, ensure_ascii=False) for r in case["recommendations"])
        blocks.append(f'{{"stem": {json.dumps(case["stem"])},\n "calc": {json.dumps(case["calc"], ensure_ascii=False)},\n'
                      f' "recommendations": [\n  {recs}]}}')
    return "[\n" + ",\n".join(blocks) + "\n]"


def plan(current: str, baseline_name: str | None) -> dict:
    versions = {current: "spending_report.narrative"}
    if baseline_name:
        versions[baseline_name] = "spending_report.narrative_base"
    steps = [{"model": m, "versions": ([baseline_name] if baseline_name and m in BASELINE_MODELS else []) + [current]}
             for m in MODELS]
    return {"versions": versions, "steps": steps}


def gguf_plan(current: str) -> dict:
    """양자화마다 한 단계. 모델 이름은 'unsloth/Qwen3-4B-Instruct-2507-Q4_K_M'처럼 원본 이름 뒤에 양자화를 붙인다."""
    owner, repo = GGUF_REPO.split("/")
    base = repo.removesuffix("-GGUF")
    steps = [{"model": f"{owner}/{base}-{quant}", "versions": [current],
              "gguf": {"repo": GGUF_REPO, "file": f"{base}-{quant}.gguf", "sha256": sha},
              "threads": GGUF_THREADS, "ctx": GGUF_CTX}
             for quant, sha in GGUF_QUANTS.items()]
    return {"versions": {current: "spending_report.narrative"}, "steps": steps}


def build(db: Path, current: str = "v3", baseline: Path | None = None, baseline_name: str = "base",
          gguf: bool = False) -> dict:
    conn = sqlite3.connect(db)
    cases = []
    for case_id in DEFAULT_CASES:
        inp = load_input(conn, case_id)
        analysis = engine.analyze_rows(inp)
        calc = engine.calculate(inp, analysis)
        cases.append({"stem": f"{inp.case_id}_{inp.profile_version}", **narrative_inputs(calc, analysis.recommendations)})
    files = {
        "spending_report/llm.py": (ROOT / "spending_report" / "llm.py").read_text(encoding="utf-8"),
        "spending_report/narrative.py": (ROOT / "spending_report" / "narrative.py").read_text(encoding="utf-8"),
        "policy_enrich/amounts.py": (ROOT / "policy_enrich" / "amounts.py").read_text(encoding="utf-8"),
        "run_bench.py": (ROOT / "colab" / "run_bench.py").read_text(encoding="utf-8"),
        "inputs.json": inputs_json(cases),
        "plan.json": json.dumps(gguf_plan(current) if gguf else plan(current, baseline_name if baseline else None),
                                ensure_ascii=False),
    }
    if baseline:
        files["spending_report/narrative_base.py"] = baseline.read_text(encoding="utf-8")
    cells = [cell("markdown", INTRO_GGUF if gguf else INTRO), cell("code", SETUP_GGUF if gguf else SETUP)]
    cells += [cell("code", f"%%writefile {path}\n{normalize(text)}") for path, text in files.items()]
    expected = json.dumps({path: short_sha(text) for path, text in files.items()}, indent=1)
    cells += [cell("code", VERIFY.replace("{expected}", expected)),
              cell("code", "!python run_bench.py"),
              cell("code", "print('RESULTS_B64:' + open('results.b64').read())")]
    return {"nbformat": 4, "nbformat_minor": 0,
            "metadata": {"accelerator": "GPU", "colab": {"gpuType": "A100", "machine_shape": "hm", "provenance": []},
                         "kernelspec": {"display_name": "Python 3", "name": "python3"}, "language_info": {"name": "python"}},
            "cells": cells}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--current-name", default="v3", help="지금 narrative.py의 버전 이름")
    parser.add_argument("--baseline", type=Path, help="함께 견줄 이전 narrative.py(선택)")
    parser.add_argument("--baseline-name", default="base")
    parser.add_argument("--gguf", action="store_true", help="노트북 PC용 양자화 모델을 CPU(llama.cpp)로 시험")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "customer_profiles.db")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or ROOT / "colab" / ("llm_eval_gguf.ipynb" if args.gguf else "llm_eval.ipynb")
    notebook = build(args.db, args.current_name, args.baseline, args.baseline_name, gguf=args.gguf)
    out.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(out, out.stat().st_size, "bytes", len(notebook["cells"]), "cells")


if __name__ == "__main__":
    main()
