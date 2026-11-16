-- (라) 고객 입력 스키마. 'AI 재무설계 자동화 조립 설명서' 2장·9장의 customer_profile 규격을 따른다.
-- 정책 DB와 섞이지 않도록 별도 파일(data/customer_profiles.db)에 둔다.
-- 금액은 모두 원(KRW) 정수. 화면에서만 만원으로 바꾼다.
-- 모든 값 행에 status / source_ref / assumptions를 둔다.
--   status: 기재(문서에 적힘) / 확인(증빙 대조) / 추정 / 불일치 / 미확인 / 해당없음
--   값을 모르면 금액 칸은 NULL, status는 '미확인'. 0원은 '0원으로 확인된 값'일 때만 쓴다.

CREATE TABLE IF NOT EXISTS profiles (
    case_id          TEXT NOT NULL,
    profile_version  TEXT NOT NULL,          -- 입력을 고치면 올린다. 이전 버전은 지우지 않는다
    as_of_date       TEXT NOT NULL,          -- 자료 기준일 YYYY-MM-DD
    is_synthetic     INTEGER NOT NULL,       -- 가상 고객 1
    customer_type    TEXT,                   -- 근로 / 사업·프리랜서 / 겸업 (소득원 목록에서 파생, 표시용)
    created_at       TEXT NOT NULL,
    note             TEXT,
    PRIMARY KEY (case_id, profile_version)
);

-- 한 번만 나오는 개인·가구 항목(생년월일, 지역, 혼인, 가구원 수, 주택 보유, 투자성향 등)
CREATE TABLE IF NOT EXISTS profile_facts (
    case_id          TEXT NOT NULL,
    profile_version  TEXT NOT NULL,
    field            TEXT NOT NULL,
    value_text       TEXT,
    value_num        REAL,
    unit             TEXT,
    status           TEXT NOT NULL,
    source_ref       TEXT,
    assumptions      TEXT,
    PRIMARY KEY (case_id, profile_version, field),
    FOREIGN KEY (case_id, profile_version) REFERENCES profiles(case_id, profile_version)
);

-- 소득원. 같은 소득원의 금액 의미(basis)마다 한 행: 연봉·총급여·매출·필요경비·사업소득금액·기납부세액·세후 입금 등
CREATE TABLE IF NOT EXISTS income_items (
    case_id          TEXT NOT NULL,
    profile_version  TEXT NOT NULL,
    source_id        TEXT NOT NULL,          -- 소득원 묶음 키(예: 근로-가상생활유통, 사업-프리랜서)
    kind             TEXT NOT NULL,          -- 근로 / 사업 / 이자 / 배당 / 연금 / 기타
    payer            TEXT,                   -- 지급처(고객사별 매출이면 고객사)
    basis            TEXT NOT NULL,          -- 금액의 의미
    period           TEXT NOT NULL,          -- 연 / 월
    tax_year         INTEGER,                -- 귀속연도
    amount_won       INTEGER,
    low_won          INTEGER,                -- '350~390'처럼 범위로 적힌 값. amount_won은 중간값
    high_won         INTEGER,
    in_cashflow      INTEGER NOT NULL DEFAULT 0,  -- 현금흐름표의 월 유입으로 쓰는 행 1
    status           TEXT NOT NULL,
    source_ref       TEXT,
    assumptions      TEXT
);

-- 세금·정산 항목(연말정산, 종합소득세 신고, 사회보험)
CREATE TABLE IF NOT EXISTS tax_items (
    case_id          TEXT NOT NULL,
    profile_version  TEXT NOT NULL,
    tax_year         INTEGER,
    filing           TEXT NOT NULL,          -- 연말정산 / 종합소득세 / 사회보험
    item             TEXT NOT NULL,          -- 총급여, 과세표준, 결정세액, 기납부세액, 월세액공제대상 ...
    period           TEXT NOT NULL DEFAULT '연',
    amount_won       INTEGER,
    status           TEXT NOT NULL,
    source_ref       TEXT,
    assumptions      TEXT
);

-- 자산(예금·적금·청약·증권·연금계좌·보증금·해약환급금)
CREATE TABLE IF NOT EXISTS assets (
    case_id                  TEXT NOT NULL,
    profile_version          TEXT NOT NULL,
    asset_id                 TEXT NOT NULL,
    kind                     TEXT NOT NULL,   -- 입출금 / 정기예금 / 적금 / 파킹 / 주택청약 / ETF / 연금저축 / 임차보증금 / 해약환급금 / 세금준비금
    name                     TEXT,
    institution              TEXT,
    opened_on                TEXT,
    matures_on               TEXT,
    principal_won            INTEGER,
    balance_won              INTEGER,         -- 기준일 잔액 또는 평가액
    monthly_contribution_won INTEGER,
    rate_pct                 REAL,
    earmark                  TEXT,            -- 용도가 정해진 돈(세금 납부 준비 등). 목표 배분에서 뺀다
    liquidity                TEXT,            -- 즉시 / 만기 / 해지손실 / 계약종료
    status                   TEXT NOT NULL,
    source_ref               TEXT,
    assumptions              TEXT,
    PRIMARY KEY (case_id, profile_version, asset_id)
);

CREATE TABLE IF NOT EXISTS liabilities (
    case_id              TEXT NOT NULL,
    profile_version      TEXT NOT NULL,
    liability_id         TEXT NOT NULL,
    kind                 TEXT NOT NULL,       -- 신용대출 / 전세대출 / 주담대 / 학자금 / 미확정세금 ...
    institution          TEXT,
    principal_won        INTEGER,
    balance_won          INTEGER,
    started_on           TEXT,
    matures_on           TEXT,
    repayment            TEXT,                -- 만기일시 / 원리금균등 / 원금균등
    rate_pct             REAL,
    monthly_payment_won  INTEGER,
    status               TEXT NOT NULL,
    source_ref           TEXT,
    assumptions          TEXT,
    PRIMARY KEY (case_id, profile_version, liability_id)
);

-- 현금흐름 유출(저축·보험·세금·고정·변동). 조립 설명서 5장의 세 축으로 분류한다
CREATE TABLE IF NOT EXISTS cashflow_items (
    case_id          TEXT NOT NULL,
    profile_version  TEXT NOT NULL,
    section          TEXT NOT NULL,           -- 저축 / 보험 / 세금·사회보험 / 고정지출 / 변동지출
    item             TEXT NOT NULL,
    monthly_won      INTEGER,
    low_won          INTEGER,
    high_won         INTEGER,
    purpose          TEXT,                    -- 생활 / 업무 / 혼합 / 미확인
    frequency        TEXT,                    -- 고정 / 변동 / 비정기
    necessity        TEXT,                    -- 필수 / 일부조정 / 선택 / 미확인 (고객 합의 전에는 미확인)
    linked_asset_id  TEXT,                    -- 저축이 쌓이는 자산(중복 집계 방지)
    detail           TEXT,                    -- 세부 내역 원문
    status           TEXT NOT NULL,
    source_ref       TEXT,
    assumptions      TEXT
);

CREATE TABLE IF NOT EXISTS insurance_contracts (
    case_id              TEXT NOT NULL,
    profile_version      TEXT NOT NULL,
    contract_id          TEXT NOT NULL,
    name                 TEXT,
    contractor           TEXT,
    insured              TEXT,
    payer                TEXT,                -- 실제 납입자. 세액공제·고객 부담 판단에 필요
    monthly_premium_won  INTEGER,
    coverage_summary     TEXT,                -- FACT FINDING 요약. 특약별 확정은 증권·약관 확인 후
    started_on           TEXT,
    pay_term             TEXT,                -- 납입기간(보장기간과 다르다)
    coverage_end         TEXT,                -- 현재 계약상 보장 종료
    renewal              TEXT,                -- 갱신·재가입 주기와 다음 시점
    surrender_value_won  INTEGER,
    status               TEXT NOT NULL,
    source_ref           TEXT,
    assumptions          TEXT,
    PRIMARY KEY (case_id, profile_version, contract_id)
);

CREATE TABLE IF NOT EXISTS goals (
    case_id          TEXT NOT NULL,
    profile_version  TEXT NOT NULL,
    goal_id          TEXT NOT NULL,
    term             TEXT NOT NULL,           -- 단기 / 중기 / 장기
    name             TEXT NOT NULL,
    due              TEXT,                    -- YYYY-MM 또는 "65세"
    amount_won       INTEGER,                 -- 목표액. 상담 산정 전이면 NULL
    amount_basis     TEXT,                    -- 일시금 / 월 생활비 / 보증금 순증 ...
    value_basis      TEXT,                    -- 현재가치 / 미래가치 / 미확인
    priority         INTEGER,
    status           TEXT NOT NULL,
    source_ref       TEXT,
    assumptions      TEXT,
    PRIMARY KEY (case_id, profile_version, goal_id)
);

-- 기능 사이 결과 규격(조립 설명서 9장 module_result). 정책·소비·보험·배분 결과를 버전별로 보관
CREATE TABLE IF NOT EXISTS module_results (
    case_id          TEXT NOT NULL,
    profile_version  TEXT NOT NULL,
    module           TEXT NOT NULL,           -- policy / spending / insurance / allocation / tax
    status           TEXT NOT NULL,           -- completed / partial / needs_review / failed
    result_json      TEXT NOT NULL,           -- findings, calculations, evidence, missing_fields, questions
    created_at       TEXT NOT NULL,
    PRIMARY KEY (case_id, profile_version, module)
);
