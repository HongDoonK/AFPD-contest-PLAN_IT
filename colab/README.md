# colab

GPU가 없는 PC 대신 Colab에서 보고서 문장(LLM) 실험을 돌리는 도구.

## 흐름

1. `python -X utf8 colab/build_notebook.py --current-name v5`로 `llm_eval.ipynb`를 만든다.
   - 실제 소스(`llm.py`, `narrative.py`, `amounts.py`, `run_bench.py`)와 고객 6명의 문장 입력을 셀에 담는다.
   - 문장 입력은 `write_narrative`가 읽는 값(금액, 비율, 권고 항목)뿐이다. 이름·주소·문서는 담지 않는다.
   - 실험할 모델은 `build_notebook.py`의 `MODELS`에서 고친다. 이전 버전과 같이 돌리려면 `--baseline <이전 narrative.py> --baseline-name v3`.
   - 온도 0이라 같은 코드·모델의 답은 늘 같다. 이미 돌린 버전은 다시 넣지 않아도 된다.
2. 노트북을 드라이브에 올리고 Colab에서 런타임 > 모두 실행. 메타데이터가 A100 고용량 RAM을 요청한다.
   - 아홉째 셀이 Colab에 쓰인 파일의 지문을 확인한다(`OK`가 아니면 옮기는 중에 내용이 바뀐 것).
   - 외부로 열리는 서버, 터널, 드라이브 연결은 쓰지 않는다.
3. 끝나면 마지막 셀에 `RESULTS_B64:`가 출력되고 노트북과 함께 저장된다. 저장된 노트북을 받아 로컬에서 정리한다.

```bash
python -X utf8 -m spending_report.llm_eval --colab-results <저장된 노트북.ipynb> --label colab_1
```

`output/llm_eval/<label>/`에 `summary.md`(모델별 채택률·거절 사유·속도), `samples.md`(문장 비교), 모델별 전체 보고서가 생긴다. 전체 보고서는 Colab에서 기록한 답을 같은 질문에 재생해 만든다(`ai_usage.provider`가 `local-replay`). 로컬 `narrative.py`가 Colab에서 돌린 v2와 다르면 재생할 답이 없어 `missing_answers`로 드러난다.

4. 런타임 > 런타임 연결 해제 및 삭제. 켜 두면 컴퓨팅 단위가 줄어든다.

## 노트북 PC용 양자화 모델(GGUF)

`python -X utf8 colab/build_notebook.py --current-name v5 --gguf`로 `llm_eval_gguf.ipynb`를 만든다. 2026-11-10 결과는 `output/llm_eval/colab_3`과 09 티켓.

- Qwen 공식 GGUF가 없어 `unsloth/Qwen3-4B-Instruct-2507-GGUF`(Apache-2.0)의 Q4_K_M, Q8_0, Q3_K_M, Q5_K_M을 받는다. 받은 파일은 `build_notebook.py`에 적은 sha256과 맞춰 본다.
- llama.cpp(`LLAMA_CPP_TAG`)를 GitHub에서 받아 CPU 전용으로 빌드하고, 서버를 런타임 안(127.0.0.1)에서만 띄워 `ServerLLM`으로 부른다. 노트북 PC에서 `LLM_PROVIDER=server`로 쓰는 경로와 같다.
- 노트북 PC(i5-1135G7, 4코어)에 맞춰 4스레드로 돌린다. Colab CPU에서 잰 속도라 노트북 PC와는 차이가 있다.
- 결과 정리는 위와 같다. `summary.md`에 프롬프트 처리·생성 속도, 최대 메모리, 최대 사용 문맥이 붙는다.
