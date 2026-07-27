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

-- ── 면책 매핑 (담보→면책조항; 보장/지급 질의 시 점수 무관 강제 첨부) ──
CREATE TABLE IF NOT EXISTS coverage_exclusion_map (
    id             BIGSERIAL PRIMARY KEY,
    product_id     TEXT REFERENCES product(product_id),
    coverage_name  TEXT,
    coverage_clause TEXT REFERENCES clause(clause_id),   -- 지급사유 조(제5조)
    exclusion_clause TEXT REFERENCES clause(clause_id),  -- 면책/감액 조(제7조·제6조⑤)
    kind           TEXT                         -- 'general'(면책) | 'reduction'(감액)
);
