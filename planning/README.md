# planning

정책 매칭(`customer/`)과 보고서(`spending_report/`) 사이의 재무 컨설팅 로직. 첫 조각은 4대 생애목표(주거·결혼·양육·노후) 자산 분배 비율이다. 계산은 모두 결정적 코드이고 LLM은 쓰지 않는다.

```bash
python -X utf8 -m planning.allocation --step2 planning/cases/kim_doyun_step2.json \
    --case customer/cases/kim_doyun.json --out output/allocation_kim_doyun_v1.json
```

## 입력

- step2 JSON(만원): `p`주택청약 `q`IRP `r`연금저축 `s`변액보험 잔액, `*_rate`·`*_date`·`delta*`(연 납입), `M` 현재자산, `deltaM` 연 잉여현금, `_meta.deltaM_detail.income_monthly`, `_meta.asset_detail.M_items`
- 고객 프로필: `customer.profile.load_case`로 읽은 facts(생년월일, 성별, 시도, 혼인, 주택 보유, `children_count`가 있으면 자녀 수). 나이는 프로필 `as_of_date` 기준 만 나이.

## 규칙

1. `M_avail`: 기본은 M이 이미 p·q·r·s를 뺀 금액(`m_includes_locked=False`). True면 M − (p+q+r+s). 두 값을 모두 남기고 `M_items` 합이 M과 다르면 경고.
2. 보존 자산 표: p·q·r·s별 잔액·연이율·가입일·경과 연수·연 납입액. null은 null 그대로.
3. 비상금 = 월 소득 × 1.5(범위 1.0~1.5), M_avail을 넘으면 M_avail까지. 분배 대상 D = M_avail − 비상금.
4. 목표별 need / existing / gap = max(need − existing, 0) / target_age / years / `horizon_source` / active.
   - 목표 시점(v2): 프로필 `goals`에서 이름에 주거·주택·내 집 / 결혼·웨딩·혼인 / 양육·자녀·육아·출산 / 은퇴·노후가 들어간 목표의 `due`('YYYY-MM-DD', 'YYYY-MM', 'YYYY', 'N세')를 먼저 쓰고, 없으면 통계 연령. years 하한 1년. `horizon_source`에 출처와 대응 규칙.
   - 주거: 매매가 − 대출 + 매매가 × 부대비용률, 대출 = min(매매가 × LTV, 6억원)이고 상한은 서울·경기·인천만. existing = p. 주택 보유면 제외. 시도값이 없으면 전국값.
   - 결혼: 주거비 제외 결혼 비용 × 본인 부담(0.5), 범위 함께. 성별 평균 초혼 연령. 기혼이면 제외.
   - 양육: 자녀 1인당 출생~대학 양육비(2012년) × 물가 계수(CPI 2025/2012) × 본인 부담(0.5) × 자녀 수(없으면 1명). years = 출산까지 연수(자녀가 있으면 0) + 지출기간 22년/2.
   - 노후: 생활비 × 12 × max(65 − 은퇴, 0) + max(생활비 − 연금, 0) × 12 × (60 + 60세 기대여명 − max(은퇴, 65)). 최소 생활비 기본, 적정 생활비 범위 상단. existing = q+r+s.
5. `weight = gap / years`, `ratio = weight / Σweight`(active만), 참고 `ratio_by_need = gap / Σgap`. 비율은 0.1% 단위 최대 잔여법으로 합 100.0, 금액은 0.01만원 단위 최대 잔여법으로 합 D.
6. 달성 가능성(표시만): `coverage = amount / gap`, `monthly_needed = (gap − amount) / (years × 12)`, Σmonthly_needed와 deltaM/12 비교.

## 정책 지출 절감 (`spending_cut.py`)

```bash
python -X utf8 -m planning.spending_cut --case customer/cases/kim_doyun.json --policy output/policy_kim_doyun_v2.json \
    --step2 planning/cases/kim_doyun_step2.json --out output/spending_cut_kim_doyun_v1.json
```

- `customer.spending_handoff.build()`를 범주 확장 인자(`categories=EXT_CATEGORIES`, `other_filter=ext_other_filter`)로 다시 불러 정책을 지출 항목에 붙인다. 기본 호출 결과는 바뀌지 않는다.
- 추가 범주는 식비 하나(생활비·식비 항목, 직장인 점심 외식 할인 등). 2026-11 조사에서 지역화폐·상품권 할인형은 고객 6명 모두 0건이라 넣지 않았다.
- 항목별 `before`·`certain_saving`(입력상 충족)·`potential_saving`(확인 필요 포함)·`after_*`, 정책 목록. 절감은 지출액(세부 내역의 적용 부분)을 넘지 않고, 항목마다 가장 큰 정책 하나만, 금액 모름은 null.
- step2가 있으면 변동(step2 `variable_after_monthly`+`cut_monthly`)·고정(`fixed_after_monthly`, 고정지출+보험)과 5% 넘게 다르면 경고, 변동 정책 절감 상한은 `variable_after_monthly`. `deltaM_after = deltaM + 연간 효과`, 그리고 `allocate()` 달성 가능성 전후(`allocation_effect`).
- `--policy`가 없으면 두 DB를 읽기 전용으로 열어 `customer.policy_match.match()`로 판정한다(저장하지 않음).
- 상황형 정책은 절감에 넣지 않고 `to_check`(항목당 5건)에만 둔다.

## 정책 반영본 step2 (`reflect.py`)

```bash
python -X utf8 -m planning.reflect --step2 planning/cases/kim_doyun_step2.json --case customer/cases/kim_doyun.json \
    --policy output/policy_kim_doyun_v2.json --scenario both --out "output/step2_reflected_kim_doyun_{scenario}_v1.json"
```

- 팀원 step2와 같은 최상위 키·순서. `deltaM`만 `spending_cut`의 `deltaM_after[scenario]`로 바꾸고 나머지 값은 그대로.
- `_meta.deltaM_detail`: `a_monthly`(= 새 deltaM/12)·`fixed_after_monthly`·`variable_after_monthly` 반영, `policy_cut_monthly` 추가. 지원 기간 12개월 미만 정책 때문에 income − fixed − variable ≠ a_monthly이면 `messages`에 한 줄.
- `_allocation`: 반영 후 deltaM으로 부른 `allocate()` 요약과 `feasibility_before`.
- `_meta.policy_reflection`: 시나리오, 출처, 전후·변화, 반영한 정책, 반대 시나리오, 반영하지 않은 것 건수, 규칙.
- `reflect()`는 순수 함수(입력 불변). 이미 반영된 step2, step2 없이 만든 cut, 잘못된 scenario는 ValueError. `--force` 없이 덮어쓰지 않음.

## 상담 대시보드 파이프라인 (`step2_build.py`, `strategy.py`, `advisor.py`, `pipeline.py`, `dashboard.py`)

```bash
python -X utf8 -m planning.pipeline --case kim_doyun --step2 planning/cases/kim_doyun_step2.json --scenario certain
python -X utf8 -m planning.pipeline --case blank_01                      # 팀원 step2 없이(build_step2)
python -X utf8 -m planning.pipeline --case new_01 --docs <고객 문서 폴더>  # FF·원천징수영수증에서 바로
LLM_API_KEY=... python -X utf8 -m planning.pipeline --case kim_doyun --step2 planning/cases/kim_doyun_step2.json     --llm-provider anthropic --llm-model claude-sonnet-5-5 --llm-base-url https://<host>/v1/gateway/claude --suffix _llm
LLM_API_KEY=... LLM_REASONING_EFFORT=none python -X utf8 -m planning.pipeline --case kim_doyun \
    --step2 planning/cases/kim_doyun_step2.json --llm-provider server --llm-model gpt-6-luna \
    --llm-base-url https://<host>/<OpenAI 호환 경로> --llm-fallback-provider server --suffix _llm
```

산출물은 `output/plan_<case>_<scenario>_v1.json`(근거 전체), `dashboard_<case>_<scenario>_v1.html`(재무설계사용 한 파일), `ai_log_plan_<case>_<scenario>_v1.json` 세 개뿐이다. `--force` 없이 덮어쓰지 않는다. 중간 결과는 메모리로 넘기고, 정책 DB는 `?mode=ro`로 연다. 정책 판정·소비 보고서가 DB 형태 입력을 받으므로 프로필을 메모리 SQLite(`:memory:`)에 넣어 쓴다. `customer_profiles.db`와 `module_results`에는 쓰지 않는다. 한 단계가 실패하면 `[단계] 단계에서 멈춤: 원인` 한 줄로 끝난다(종료 코드 1). 단계별 상태·소요·경고는 `plan.pipeline.steps`. `--suffix`는 산출물 파일명 끝에 붙어 템플릿 판을 덮어쓰지 않는다. LLM 호출별 소요 시간과 입력·출력 토큰 수는 `plan.advice.call_stats`·`totals`와 ai_log의 `calls`에 남는다.

순서: (문서 → `from_documents.build_case`, 임시 파일) → `load_case` → `policy_match.match` → `spending_handoff.build` → `spending_report.report.build_report` → step2(파일 또는 `build_step2`) → `spending_cut` → `reflect.build` → `build_strategy` → `advise` → `dashboard.render`.

### 주 모델 → 대체 모델 → 템플릿

환경변수를 주지 않으면 예전과 같다(대체 없음). `LLM_FALLBACK_PROVIDER`를 주면 `get_client`가 `FallbackLLM`으로 감싼다.

| 환경변수 | 값 |
|---|---|
| `LLM_REASONING_EFFORT` | 주 모델(OpenAI 호환, 예: gpt-6-luna)의 `reasoning_effort`(none, low 등). 주면 `max_completion_tokens`를 쓴다 |
| `LLM_REASONING_HEADROOM` | 주 모델 토큰 한도에 더할 여유. 기본 0 |
| `LLM_FALLBACK_PROVIDER` | `none`(기본), `server`, `local`, `anthropic` |
| `LLM_FALLBACK_MODEL` | 기본 `Qwen/Qwen3-4B-Instruct-2507` |
| `LLM_FALLBACK_BASE_URL` | 기본 `http://localhost:8080/v1` |
| `LLM_FALLBACK_API_KEY` | 보조 모델 키(선택). 환경변수로만 |

- 주 모델이 소진형 오류(HTTP 401·402·403·429, 또는 본문에 quota·insufficient·credit·balance·exceeded·limit·한도·소진·잔액)를 내면 그 뒤로 보조 모델만 쓴다. 429는 일시 재시도 후에도 실패한 경우다.
- 그 밖의 오류(시간 초과, 빈 응답, 5xx)는 그 호출만 보조 모델로 넘기고, 주 모델이 연속 3회 실패하면 보조 모델로 영구 전환한다.
- 보조 모델에는 `LLM_REASONING_*`를 쓰지 않는다(Qwen 요청 본문은 예전과 같다). 보조 모델도 실패하면 `LLMUnavailable`이 나가고, 소비 보고서·에이전트는 남은 문장을 템플릿으로 쓴다.
- 기록: ai_log와 plan `advice`에 호출별 실제 공급자·모델(`role`: primary·secondary), 전환 여부·사유·시점(`fallback`), 문장 출처(`sentence_sources`). 대시보드 AI 기록에 "주 모델 N문장, 대체 모델 M문장, 템플릿 K문장"과 전환 사유. llm_eval summary에 모델별 `fallback_calls`.

### 목표 기준(`goals.py`, `--goal-basis`, `--extra-goal`)

- `--goal-basis customer`(기본): Fact Finding '2. 재무목표'를 기준으로 분배한다. `statistical`은 예전 4대 목표 통계 방식이다. `allocate()`는 인자를 주지 않으면 예전과 같다(`goal_basis="statistical"`).
- 분류(`classify_goals`): 비상금·예비금 → 비상금, 대출·상환·부채 → 부채 상환, 양육·자녀·교육자금·학자금 → 양육, 주거·주택·자가 → 주거, 결혼 → 결혼, 은퇴·노후 → 노후, 그 밖 → 기타. 고객 표기 기간(`term_stated`), 기한, 금액(만원), 원문(`raw`, 문서에서 만든 프로필만)을 남긴다. '매년', '1년에 한 번', '연 1회', '해마다'가 있으면 매년 반복 목표.
- 비상금: 고객이 적었으면 그 금액(M_avail 상한), 아니면 월 소득 × 1.5. 분배 대상 D = M_avail − 비상금.
- 분배 목표 = 4대 기본 목표(항상, 기혼 → 결혼 제외, 주택 보유 → 주거 제외) + 추가 목표(부채 상환, 기타, 자녀 정보가 없는 고객의 교육자금, CLI 목표). 공식은 같다(weight = gap / years).
  - 4대 목표: 고객이 적었으면 고객 금액·시점(`need_source='고객 금액'`), 금액이 없으면 통계값(`'통계 대체'`), 적지 않았으면 통계값·통계 연령(`'통계 기본값'`, `unstated_goals`에도 기록). 주거 금액은 자기자금 목표(existing = p), 노후 '월 생활비'는 노후 산식에 생활비만 고객 금액으로 바꿔 계산. `reference_need`는 같은 종류의 통계값.
  - 매년 반복 목표: need = 1회 금액, years = 다음 시점까지(없으면 1년). 목표 월 필요액에는 금액 ÷ 12 이상을 넣는다.
- `--extra-goal "이름|시점|금액(만원)|annual"`(여러 번)로 프로필을 고치지 않고 추가 목표를 넣어 본다. `goals_input`에 출처 'CLI 추가'.
- `reflect.build`·`spending_cut`에도 같은 모드가 넘어가 달성 가능성 전후가 일관된다(기존 CLI 기본 동작은 그대로).

### 수익률·자산 배정·양육 전환·전략 원칙(2026-11-15 재설계)

- 수익률(사용자 지정, `reference.STRATEGY_REFERENCE`): 단기(남은 기간 3년 이하 목표·비상금·부채 상환) 연 2~3%(기준 2.5%), 그 밖의 3년 초과 목표(노후 제외) 5~8%(6.5%, 3년 이하가 되면 단기), 노후는 은퇴까지 20년 초과 70:30·10% 내외, 20년 이하 10년 초과 50:50·7~9%, 10년 이하 30:70·5%. 25~20년 구간은 70:30으로 처리(확인 필요). 하한·기준·상한 세 경로, 실질 = (1+명목)/(1+물가)−1, `--inflation`(기본 2.0). 투자성향은 계산에 쓰지 않고 확인 사항으로만.
- 자산 배정: 안전(입출금·예금·적금·CMA 등)과 투자(ETF·펀드·주식·ISA) 구분. 비상금과 3년 안 목표는 안전 자산에서만(목표 시점 전 만기 우선), 투자 자산은 남은 기간이 긴 목표부터, 남는 안전 자산은 3년 넘는 목표 중 시점이 빠른 순. 모자라면 `rebalance`. 보존자산(청약 → 주거, IRP·연금저축·변액 → 노후)도 표에.
- 양육 전환: 전환 시점 T_c(고객 양육·출산 시점, 없으면 통계상 출산 예상 시점)까지는 양육 포함 비율, 그 뒤 양육은 목표에서 빼고 월 양육비(서울가정법원 양육비 산정기준표 2021 개정, 본인 세전 월소득 구간, 여섯 나이 구간 평균 × 자녀 수 계수 × 본인 부담 0.5)를 변동지출로 처리, 남은 목표 비율을 같은 공식(전환 시점 예상 잔액 기준 부족분 ÷ 남은 기간)으로 다시 계산. 자녀 19세부터 월 잉여 복귀, 비율 유지. 이미 자녀가 있으면 양육은 처음부터 제외(양육비는 현재 지출), 교육자금은 추가 목표. `strategy.phases`.
- 전략 원칙: `planning/knowledge/strategy_guide.json`(팀 예시 보고서 요약, P01~P19). 카드마다 `principles`를 결정적으로 붙이고, 에이전트는 기간별로 후보 안에서 2~4개를 고른다(아니면 후보 상위 3개). 대시보드에는 원칙 문구를 그대로 표시.

### 단계별 비율 재계산과 고객 상황 반영(2026-11-16)

- `strategy.rebalance_phases`: 목표 달성·목표 시점 도래·양육 전환·자녀 19세가 올 때마다 새 단계. 남은 목표만으로 gap = need − 그 시점 기준 잔액, years = 남은 기간(하한 1년), weight = gap/years, 0.1% 최대 잔여법. 달성한 목표는 적립 0(잔액은 목표 시점까지 굴림), 시점이 왔는데 못 채운 목표는 `shortfall_at_due`. 남은 목표가 없으면 '미배정 여유'. 사건은 기준 경로로 정하고 같은 적립 일정을 하한·상한에도 적용. 첫 단계는 분배 비율(시작 달에 사건이 있으면 공식).
- customer 모드 가용 금융자산 = M − 용도 지정(세금준비금·earmark) 합. 배정표에는 '용도 지정' 행으로만.
- 소득 불규칙(근로소득 아님) 고객이 비상금 금액을 적지 않았으면 비상금 = 월 지출(고정+보험+변동+세금·사회보험) × 12개월(가용 자산 상한).
- facts `marriage_plan`·`children_plan`이 '없음' 계열이면 결혼·양육 기본 목표 제외(양육 전환도 없음). `from_documents`는 결혼계획 칸의 '계획 없음'·'없음'·'비혼'을 읽어 `marriage_plan`을 넣는다.
- 기한 범위('2031~2036')는 이른 해로 계산하고 늦은 해 기준 필요 월 적립·충족률을 주거 카드와 판단 사항에 함께 둔다.

### step2 유도 (`step2_build.build_step2(profile, spending_report=None)`)

- 팀원 step2와 같은 키·순서. p 주택청약, q IRP·퇴직연금, r 연금저축, s 변액보험 잔액 합. M은 그 밖의 금융자산(입출금·예금·적금·ETF·펀드·주식·CMA·파킹·ISA·세금준비금 등). 임차보증금·자가주택·해약환급금은 제외(`asset_detail.excluded_items`).
- `M_items`는 끝 숫자가 금액이 되게 쓴다(allocation이 끝 숫자를 읽음). 이름 속 숫자는 뺀다.
- deltaM = (월 소득 − 고정(고정지출+보험) − 변동) × 12. 지출 구분은 `spending_cut.SECTIONS`. 월 소득은 현금흐름 수입 행 합에서 `세금·사회보험` 구역을 뺀 값(프리랜서의 세전 매출 입력 대응).
- 소비 보고서가 있으면 조정액(`calculations.effect.adjustment_monthly_won`) 전체를 변동지출에서 빼고 `cut_monthly`에 기록.
- 잔액·지출 미확인은 0으로 두고 경고, `_meta.status`·`deltaM_detail.status`는 INCOMPLETE, 목록은 `_meta.unknown`.
- 김도윤: p·q·r·s·M = 300·0·500·0·4,700(팀원과 같음), deltaM 1,760.4(팀원 1,783.2, 고정·변동 분류와 행동 절감액 차이).

### 전략 엔진 (`strategy.build_strategy(reflected_step2, profile, policy_result, cut)`)

- LLM 없이 모든 숫자를 만든다. 가정·제도값은 `reference.STRATEGY_REFERENCE`(안전 2.5%, 위험 6.5%, 물가 2.0%, 위험 비중 경로, 투자성향 계수, 대출 4.5%·30년·스트레스 +3.0%p, DSR 40%, ISA, 연금계좌 세액공제). 기존 `REFERENCE`는 시험이 키 집합과 check 어휘를 고정하고 있어 따로 둔다.
- 전망은 실질 수익률 (1+r)/(1+물가)−1, 월 복리·월말 적립, 매년 초 남은 기간으로 위험 비중을 다시 정한다(≤3년 0%, ≤5년 20%, ≤10년 40%, 그 위 60%, × 투자성향 계수, 상한 80%). 기본(`base`)과 전액 안전(`safe`) 두 경로.
- 시작 잔액 = 분배액 + 기존 보존자산(주거 p, 노후 q+r+s). 계획 월 적립 = 반영 후 `a_monthly` × 비율. 필요 월 적립은 FV(시작, x) = A + xB로 해석적으로. 달성 시점은 같은 월 적립을 계속하는 월 단위 모의(최대 60년).
- `options`: 비율 배분 대 시점순 집중(월 잉여 전액을 목표 시점이 가까운 목표부터). `asset_mapping`: M 자산을 유동성 순(즉시 → 만기 빠른 순 → 그 밖, 용도 지정 자산은 맨 뒤)으로 비상금 → 목표 시점 순에 채운다. 합계 = 프로필 M 자산 합.
- 카드: S1 비상금, S2 부채, S3 3년 안 만기, S4 지출 절감, S5 정책 신청, S6 결혼, M1 주거, M2 주택 대출(간이 DSR), M3 중기 운용·ISA, L1 연금계좌 세액공제, L2 노후, L3 양육, L4 보험. 목표 카드(M1·S6·L2·L3)의 기간은 실제 남은 기간으로 정한다. 카드는 검토 제안이며 판정하지 않는다.

### 에이전트 (`advisor.advise(strategy, client)`)

- 클라이언트는 `spending_report.llm.get_client()`(기본 none: 호출 없이 템플릿·priority).
- 기간별 카드 요약(id·제목·숫자)으로 `{"order", "focus"}`를 받고, order가 그 기간 카드 id의 순열이 아니면 버린다. 기간별 서술과 전체 요약은 템플릿 초안을 다듬게 하고 `check_text`로 검증, 한 번 재시도 후 템플릿.
- 프롬프트에는 이름·주소·기관명·정책명·문서 경로를 넣지 않는다. 호출마다 단계·프롬프트 해시·응답·채택/거부 사유를 로그에 남긴다.

### 대시보드 (`dashboard.render(plan)`, `templates/dashboard.html.j2`)

머리(고객 요약·시나리오·검토 상태) → 핵심 지표 6칸 → 설계사 판단 사항·경고·물어볼 것 → 재무상태(자산 구성, 목적별 배정) → 현금흐름 전후 → 목표별 분배 → 단기·중기·장기 전략(서술, 카드, 전망 선 그래프) → 대안 비교 → 정책 → 가정·한계·AI 기록. 외부 리소스 없는 한 파일, 인라인 SVG(서버 측 좌표), autoescape, 좁은 화면에서 한 열, 인쇄 시 카드가 쪽 사이에서 잘리지 않음.

## 가정과 한계

- 기준값은 `reference.py`에 출처·확인일·`kind`(통계/가정)·`check`(원문 확인/기사 확인/가정/못 찾음)와 함께 둔다. 기사로만 확인한 값이 많다.
- 시도별 평균 매매가격은 서울·경기만 있다(2026년 5월). 나머지 15개 시도는 전국값(4억3,830만원)으로 대체하고 경고를 남긴다.
- 양육비 환산은 2025년 연간 CPI까지다. 노후 생활비·연금은 2024~2026년 값이며 노후 기간 물가·수익률은 반영하지 않는다.
- 고객 목표의 은퇴 시점(예: 65세)은 years에만 쓰고, 필요자금은 `retire_age`(기본 60)로 계산한다. 다르면 경고.
- 고가주택(15억 초과) 대출 상한 축소, DSR은 반영하지 않는다.
- 목표 시점이 1년 안이거나 지났으면 years=1로 두어 연간 필요 적립액이 커진다(경고로 표시).
- 전략 엔진의 수익률·물가·위험 비중은 가정값이고, ISA·연금 세액공제 값은 원문 확인이 필요하다. DSR은 새 주택 대출만 넣은 간이 계산이다.
