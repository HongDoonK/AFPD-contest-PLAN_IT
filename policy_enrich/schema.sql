-- 원본 youth_policy.db에 덧붙이는 테이블. 원본 테이블은 건드리지 않는다.

-- (가) 정책 소득·자격 조건
CREATE TABLE IF NOT EXISTS policy_conditions (
    policy_id                     TEXT PRIMARY KEY REFERENCES policies(policy_id),
    income_type                   TEXT,     -- 무관 / 연소득 / 기타 (API 코드 해석)
    income_basis                  TEXT,     -- 무관 / 본인 / 가구 / 본인+가구 / 미상
    personal_median_pct_max       REAL,     -- 본인(청년) 기준 중위소득 상한 %
    household_median_pct_max      REAL,     -- 가구(원가구) 기준 중위소득 상한 %
    annual_income_max_won         INTEGER,  -- 본인 연소득 상한(대상별 기준이 있으면 가장 높은 값)
    youth_annual_income_max_won   INTEGER,  -- 대상별 기준 중 '청년' 기준(청년 5천만/청년 외 6천만 등)
    couple_annual_income_max_won  INTEGER,  -- 부부합산 연소득 상한
    monthly_income_max_won        INTEGER,  -- 월소득 상한(원문에 적힌 환산액)
    asset_max_won                 INTEGER,  -- 재산 상한
    health_insurance_max_won      INTEGER,  -- 건강보험료(직장가입자) 상한
    requires_earned_income        INTEGER,  -- 근로·사업소득 또는 재직 요건 1/0
    parse_confidence              TEXT,     -- high / partial / none / not_applicable
    income_text                   TEXT,     -- 파싱에 쓴 원문 일부(검수용)
    recruit_status                TEXT,     -- open / always / closed / unknown (as_of 기준)
    apply_end_date                TEXT,     -- YYYYMMDD
    dedup_key                     TEXT,     -- 같은 사업 묶음 키
    as_of                         TEXT
);
CREATE INDEX IF NOT EXISTS idx_policy_conditions_status ON policy_conditions(recruit_status);
CREATE INDEX IF NOT EXISTS idx_policy_conditions_dedup ON policy_conditions(dedup_key);

-- (나) 정책 혜택 금액
CREATE TABLE IF NOT EXISTS policy_benefits (
    policy_id               TEXT PRIMARY KEY REFERENCES policies(policy_id),
    benefit_type            TEXT,     -- 매칭저축 / 저축상품 / 이자지원 / 보증료지원 / 대출 / 월세·주거비 / 할인·환급 / 현금지원 / 바우처·현물 / 교육·서비스 / 기타
    monthly_amount_won      INTEGER,  -- 매월 받는 금액(매칭저축이면 매칭액)
    duration_months         INTEGER,
    total_amount_won        INTEGER,  -- 월액 x 기간, 회당 x 횟수, 원문의 최대·총액, 또는 유일한 일시금
    amount_min_won          INTEGER,  -- 본문 혜택 금액 범위(대상별·차수별로 갈릴 때). 자격 조건 금액은 제외
    amount_max_won          INTEGER,
    own_saving_monthly_won  INTEGER,  -- 매칭저축: 본인 월 저축액
    match_monthly_won       INTEGER,  -- 매칭저축: 월 매칭액
    match_ratio             REAL,     -- 매칭저축: 본인 1 대비 지원 배수
    loan_limit_won          INTEGER,
    interest_support_pct    REAL,     -- 이자지원 %(p)
    discount_pct            REAL,     -- 지출액의 N% 할인·환급·지원(대상별이면 청년 비율)
    discount_won            INTEGER,  -- 1회·1건 정액 할인
    summary_line            TEXT,
    parse_confidence        TEXT      -- high / partial / none
);
CREATE INDEX IF NOT EXISTS idx_policy_benefits_type ON policy_benefits(benefit_type);

-- (나) 직접 확인한 중앙정부 자산형성 상품
CREATE TABLE IF NOT EXISTS asset_products (
    product_id               TEXT PRIMARY KEY,
    product_name             TEXT,
    operator                 TEXT,
    status                   TEXT,
    age_min                  INTEGER,
    age_max                  INTEGER,
    age_note                 TEXT,
    term_months              INTEGER,
    monthly_limit_won        INTEGER,
    gov_match_pct_general    REAL,
    gov_match_pct_preferred  REAL,
    tax_free                 INTEGER,
    income_condition         TEXT,
    apply_window             TEXT,
    channels                 TEXT,
    source_url               TEXT,
    verified_at              TEXT,
    notes                    TEXT
);

-- 기준 중위소득(월, 원)
CREATE TABLE IF NOT EXISTS median_income (
    year            INTEGER,
    household_size  INTEGER,
    monthly_won     INTEGER,
    PRIMARY KEY (year, household_size)
);

-- (다) 금융상품(예금·적금·대출은 금감원 API, youth_loan은 smallloan_youth 이관)
CREATE TABLE IF NOT EXISTS fin_products (
    product_key     TEXT PRIMARY KEY,   -- {type}:{fin_co_no}:{fin_prdt_cd}[:{신용대출 유형}]
    product_type    TEXT,               -- deposit / saving / rent_loan / credit_loan / mortgage_loan / youth_loan
    top_fin_grp_no  TEXT,
    fin_co_no       TEXT,
    kor_co_nm       TEXT,
    fin_prdt_cd     TEXT,
    fin_prdt_nm     TEXT,
    join_way        TEXT,
    join_member     TEXT,
    join_deny       TEXT,
    max_limit_won   REAL,
    spcl_cnd        TEXT,
    mtrt_int        TEXT,
    etc_note        TEXT,
    loan_type_nm    TEXT,               -- 신용대출: 일반신용대출 / 마이너스한도대출 등
    loan_limit_text TEXT,               -- 대출한도 원문("최대4.44억원", "LTV 70%")
    erly_rpay_fee   TEXT,               -- 중도상환수수료
    dly_rate        TEXT,               -- 연체이자율
    loan_inci_expn  TEXT,               -- 대출 부대비용
    cb_name         TEXT,               -- 신용대출 금리 구간의 신용평가사
    dcls_month      TEXT,
    dcls_strt_day   TEXT,
    dcls_end_day    TEXT,
    collected_at    TEXT,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_fin_products_type ON fin_products(product_type);

CREATE TABLE IF NOT EXISTS fin_product_options (
    product_key        TEXT REFERENCES fin_products(product_key),
    intr_rate_type_nm  TEXT,
    rsrv_type_nm       TEXT,
    save_trm_months    INTEGER,
    intr_rate          REAL,
    intr_rate2         REAL,
    raw_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_fin_product_options_key ON fin_product_options(product_key);

-- (다) 대출 금리. 전세·주담대는 상환방식·금리유형별 최저/최고/평균, 신용대출은 신용점수 구간별 금리
CREATE TABLE IF NOT EXISTS fin_loan_options (
    product_key        TEXT REFERENCES fin_products(product_key),
    mrtg_type_nm       TEXT,   -- 주담대 담보: 아파트 / 아파트외
    rpay_type_nm       TEXT,   -- 분할상환방식 / 만기일시상환방식
    lend_rate_type_nm  TEXT,   -- 고정금리 / 변동금리 (신용대출: 대출금리 / 기준금리 / 가산금리 / 가감조정금리)
    credit_grade       TEXT,   -- 신용대출: "900점 초과" ~ "300점 이하", "평균"
    rate_min           REAL,
    rate_max           REAL,
    rate_avg           REAL,
    raw_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_fin_loan_options_key ON fin_loan_options(product_key);

-- 에이전트가 바로 쓰는 뷰: 지금 신청 가능한 정책 + 조건 + 혜택
CREATE VIEW IF NOT EXISTS v_policy_finance AS
SELECT p.policy_id, p.policy_name, p.category_main, p.category_sub, p.region,
       p.min_age, p.max_age, p.application_url,
       c.recruit_status, c.apply_end_date, c.income_basis,
       c.personal_median_pct_max, c.household_median_pct_max,
       c.annual_income_max_won, c.youth_annual_income_max_won, c.couple_annual_income_max_won,
       c.monthly_income_max_won,
       c.asset_max_won, c.requires_earned_income, c.parse_confidence AS condition_confidence,
       b.benefit_type, b.monthly_amount_won, b.duration_months, b.total_amount_won,
       b.amount_min_won, b.amount_max_won,
       b.own_saving_monthly_won, b.match_ratio, b.loan_limit_won, b.interest_support_pct,
       b.discount_pct, b.discount_won,
       b.summary_line, b.parse_confidence AS benefit_confidence, c.dedup_key
FROM policies p
JOIN policy_conditions c USING (policy_id)
JOIN policy_benefits b USING (policy_id);

-- 상품별 최고금리 요약
CREATE VIEW IF NOT EXISTS v_fin_product_best_rate AS
SELECT f.product_key, f.product_type, f.kor_co_nm, f.fin_prdt_nm, f.join_deny, f.max_limit_won,
       o.save_trm_months, o.rsrv_type_nm, MAX(o.intr_rate) AS base_rate, MAX(o.intr_rate2) AS max_rate
FROM fin_products f JOIN fin_product_options o USING (product_key)
GROUP BY f.product_key, o.save_trm_months, o.rsrv_type_nm;

-- 대출 상품 금리 요약. 신용대출은 실제 적용 금리(대출금리) 행만 쓴다
CREATE VIEW IF NOT EXISTS v_fin_loan_rate AS
SELECT f.product_key, f.product_type, f.kor_co_nm, f.fin_prdt_nm, f.loan_type_nm, f.loan_limit_text,
       o.mrtg_type_nm, o.rpay_type_nm, o.lend_rate_type_nm, o.credit_grade,
       o.rate_min, o.rate_max, o.rate_avg
FROM fin_products f JOIN fin_loan_options o USING (product_key)
WHERE f.product_type <> 'credit_loan' OR o.lend_rate_type_nm = '대출금리';
