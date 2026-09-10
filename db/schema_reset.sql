-- ============================================================
-- ⚠⚠ 파괴적 초기화 — 문서 계열 테이블을 **전부 지운다**.
--
-- 이걸 실행하기 전에 반드시:
--   1) 정말 초기화가 목적인가? 컬럼 추가라면 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
--      를 schema.sql 에 넣고 그걸 돌린다(비파괴).
--   2) 백업했는가?
--        docker compose exec -T postgres pg_dump -U docsrag docsrag > backup.sql
--
-- 참고: 보험 관계형(product·clause·payout_rule·coverage_range·annex*)은 여기 없다.
--       그쪽은 db/schema_insurance.sql 소관이고, 이 목록에 넣지 않는 것이 의도다.
--
-- 사용:
--   cat db/schema_reset.sql | docker compose exec -T postgres psql -U docsrag -d docsrag
--   cat db/schema.sql       | docker compose exec -T postgres psql -U docsrag -d docsrag
-- ============================================================

DROP TABLE IF EXISTS tb_page_triage;
DROP TABLE IF EXISTS tb_query_feedback;
DROP TABLE IF EXISTS tb_document_contents;
DROP TABLE IF EXISTS tb_document_chunks;
DROP TABLE IF EXISTS tb_document_extract;
DROP TABLE IF EXISTS tb_document_status;
DROP TABLE IF EXISTS tb_document_status_log;
DROP TABLE IF EXISTS tb_code_master;
DROP TABLE IF EXISTS tb_service_code;
