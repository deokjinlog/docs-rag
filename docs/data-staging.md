# 데이터 스테이징 (raw → processed → DB)

원본 추출은 **비싸고**(OCR 분 단위) 청킹은 **자주 바꾸는 실험면**이라, *비싼 상류는 저장·싼 하류는
재생성* 원칙으로 단계를 나눈다. 청크가 raw에 들어가지 않는 이유이자, 청킹 파라미터를 바꿀 때
PDF를 재추출하지 않는 이유.

> 리터럴 이름(raw/processed)을 쓰지만, 개념은 업계 표준 **Medallion Architecture**(Databricks)와 같다 —
> `raw = bronze`, `processed = silver`, `DB = gold`. 팀이 익숙한 이름을 쓰되 표준 참조만 남긴다.

## 계층

| 층 (=medallion) | 경로 | 담는 것 | 성격 |
|---|---|---|---|
| **raw** (bronze) | `data/output/raw/{doc}.md`·`.json`·`_images/` | ODL 원본(마크다운·레이아웃 트리·이미지·OCR) | **immutable**, 재현·감사 기준 |
| **processed** (silver) | `data/output/processed/{doc}/` | `clean.md`·`clauses.jsonl`·`profile.json` | 정제·구조화, **재청킹·재추출 소스** |
| **DB** (gold) | PostgreSQL · Qdrant | 청크(`tb_document_contents`) + 관계형(payout_rule·product·clause·annex) + 벡터 | 서빙 (파일 아님) |

## processed 산출물

- **clean.md** — 안전 최소 정규화(BOM·트레일링 공백·과다 빈줄). **추출 불변**(9종 골든 무회귀). 공격적 정규화(전각→반각 통일 등)는 *자체 골든 통과 후* `stage.normalize`에 확장(precision-first).
- **clauses.jsonl** — 조 파싱(`jo`·`title`·`text`) 캐시. 관계형·청킹 공용(비싼 파싱 1회 재사용).
- **profile.json** — 포맷 프로파일(전각/반각·복합문서·페이지수). 회사 넘어 일반화되는 '형식' 축(구조 아님).

## 생성 · 소비

```bash
python3 scripts/stage.py        # raw → processed 생성
```
- 소비: 스크립트는 `stage.doc_md(doc)` / `stage.doc_clauses(doc)`로 **processed 우선, 없으면 raw 폴백**.
- processed는 raw에서 **재생성 가능** → git 미추적(raw·golden만 기준). 청킹·정규화를 바꾸면 `stage.py`만 다시 돌린다.

## 왜 이렇게 (고수 패턴)

```
extract(PDF→MD)  느림·비쌈(OCR)      → raw 저장, 한 번만
정제·조파싱       중간               → processed 저장(재청킹 출발점)
chunk·embed·적재  자주 바꿈(A/B)      → DB 재생성
```
- **immutable raw** = 재현·감사. **regenerable processed/DB** = 실험 자유.
- processed가 **두 소비자**를 먹임: RAG 청커(`clean.md`) + 관계형 추출(`clauses.jsonl`).
- 세션 내내 흩어졌던 `<br>`·전각/반각·페이지마커 처리를 `stage.normalize` 한 곳으로 모으는 확장 지점.
