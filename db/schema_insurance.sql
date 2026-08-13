-- 보험 약관 관계형 모델 (RAG와 분리된 "정확한 데이터" 계층 — SQL 경로).
-- 설계 원칙: 값이 정해진 사실(면책기간·지급률·담보)은 벡터로 뽑지 않고 인덱싱 때 한 번
-- 뽑아 여기 적재 → 질의 때 SQL fetch(결정론). RAG는 조항 해석에만.
-- 이 테이블들은 RIST/VBRK 등 타 도메인엔 대응물이 없어 애초에 보험 전용이다.

-- ── 상품 (SQL 경로: "면책기간 며칠?" "가입연령?" 류) ──
CREATE TABLE IF NOT EXISTS product (
    product_id        TEXT PRIMARY KEY,       -- 'LINA_ICU_2024'
    company           TEXT,
    product_name      TEXT,
    contract_type     TEXT,                   -- '주계약' | '특약'
    is_renewable      BOOLEAN,
    -- 담보 (단일 담보 상품 기준; 복수는 향후 coverage 테이블로 분리)
    coverage_name     TEXT,
    payout_condition  TEXT,
    payout_table_ref  TEXT,                   -- annex_id (별표)
    -- 고정 사실 필드 (없으면 NULL — 아래 resolution_note로 '왜 NULL'을 명시)
    waiting_period_days INT,                  -- 면책/대기기간
    cooling_off_days    INT,                  -- 청약철회
    -- 주계약 관계 (특약은 주계약 준용 → 미확보 시 NULL)
    parent_policy_id  TEXT,
    resolution_note   TEXT,                   -- NULL 필드가 왜 NULL인지 (주계약 소관 등)
    source_doc        TEXT,
    created_at        TIMESTAMPTZ DEFAULT now()
);

-- ── 조 (조/항 파서 산출물; RAG 부모 회수 단위) ──
CREATE TABLE IF NOT EXISTS clause (
    clause_id      TEXT PRIMARY KEY,          -- 'LINA_ICU_2024_제5조'
    product_id     TEXT REFERENCES product(product_id),
    jo             INT,
    hang           INT,                       -- 항(①②③), 조 단위면 NULL
    title          TEXT,
    parent_id      TEXT REFERENCES clause(clause_id),
    body           TEXT
);
CREATE INDEX IF NOT EXISTS idx_clause_product ON clause(product_id, jo);

-- ── 참조 그래프 (조→조/항/별표/준용; 청킹 시점 해소 → 런타임 홉 제거) ──
CREATE TABLE IF NOT EXISTS clause_ref (
    id           BIGSERIAL PRIMARY KEY,
    src_clause   TEXT REFERENCES clause(clause_id),
    ref_type     TEXT,                        -- '조항' | '항' | '별표' | '준용'
    target       TEXT,                        -- clause_id | annex_id | (준용은 외부문서 설명)
    resolved     BOOLEAN DEFAULT true         -- 준용 대상이 코퍼스에 없으면 false (코퍼스 갭)
);

-- ── 별표 (fetch 경로: 조가 ID로 부른다. 검색 아님) ──
CREATE TABLE IF NOT EXISTS annex (
    annex_id     TEXT PRIMARY KEY,            -- 'LINA_ICU_2024_별표1'
    product_id   TEXT REFERENCES product(product_id),
    annex_no     INT,
    title        TEXT,
    kind         TEXT,                        -- 'payout' | 'formula' | 'classification'
    raw_markdown TEXT,
    summary      TEXT                          -- 이것만 임베딩(안전망), 값은 annex_row에서
);
CREATE TABLE IF NOT EXISTS annex_row (
    annex_id  TEXT REFERENCES annex(annex_id),
    row_no    INT,
    cols      JSONB                            -- 별표마다 컬럼이 달라 JSONB
);

-- ── 지급 규칙 (SQL 경로: "얼마 받아요? 언제부터 온전히?") ──
-- 지급액은 (담보×원인×경과기간)의 함수라 스칼라 컬럼 불가 → 행 분해(annex_row 논리).
-- 라이나식(텍스트 감액)·New치아식(경과기간 매트릭스) 두 인코딩을 한 테이블로 통합.
-- source='llm' 행은 LLM 폴백 출처 — 정밀도 게이트(≥0.9) 통과분만 신뢰(precision-first 규율).
CREATE TABLE IF NOT EXISTS payout_rule (
    id             BIGSERIAL PRIMARY KEY,
    product_id     TEXT REFERENCES product(product_id),
    coverage       TEXT,                        -- 담보(급부명)
    cause          TEXT,                        -- '질병' | '상해'/'재해' | '재해외' | NULL(전체)
    age_band       TEXT,                        -- '15세미만' | '15세이상' | NULL
    period_bucket  TEXT,                        -- '90일이하' | '90일초과1년미만' | '1년이상' | NULL(정률)
    rate_pct       NUMERIC,                     -- 보험가입금액의 N%
    per_unit       TEXT,                        -- '1일당' | '1회당' | '매월' | NULL
    limit_days     INT,                         -- 한도(일/회)
    reduction_rate_pct INT,                     -- 감액 지급률(라이나식 텍스트 감액)
    reduction_period   TEXT,                    -- 감액 경과기간('1년이내')
    reduction_cause    TEXT,                    -- 감액 대상 원인('재해외')
    source         TEXT,                        -- 'rule' | 'llm' (추출 출처 — LLM은 게이트 통과분만)
    evidence       TEXT                         -- 원문 근거
);
CREATE INDEX IF NOT EXISTS idx_payout_product ON payout_rule(product_id, coverage);

-- ── 면책 매핑 (담보→면책조항; 보장/지급 질의 시 점수 무관 강제 첨부) ──
CREATE TABLE IF NOT EXISTS coverage_exclusion_map (
    id             BIGSERIAL PRIMARY KEY,
    product_id     TEXT REFERENCES product(product_id),
    coverage_name  TEXT,
    coverage_clause TEXT REFERENCES clause(clause_id),   -- 지급사유 조(제5조)
    exclusion_clause TEXT REFERENCES clause(clause_id),  -- 면책/감액 조(제7조·제6조⑤)
    kind           TEXT                         -- 'general'(면책) | 'reduction'(감액)
);

-- 보장판정 서빙 테이블 (별표3 ICD 코드범위 → 담보) — judge_coverage.coverage_ranges 적재본.
-- 서빙(coverage_sql.judge_coverage)이 doc 파싱 없이 SELECT로 판정하게. load_coverage.py --load.
CREATE TABLE IF NOT EXISTS coverage_range (
    id          BIGSERIAL PRIMARY KEY,
    product_id  TEXT REFERENCES product(product_id),
    coverage    TEXT,
    code_token  TEXT
);
CREATE INDEX IF NOT EXISTS idx_coverage_range_product ON coverage_range(product_id);
