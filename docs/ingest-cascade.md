# 인제스트 캐스케이드 — 적응형 라우팅 설계

**목표**: "더 잘 읽는다"가 아니라 **어떤 PDF 가 들어와도 안 깨지고, 비싼 경로는 필요한
페이지에만 쓴다.** 지금은 모든 PDF 를 ODL 한 경로로 보내는데, 네이티브 PDF 뿐이라 맞지만
스캔본 특약이나 팩스 이미지가 들어오면 **조용히 빈 텍스트**가 나온다.

## 티어

```
Tier 0  NATIVE          텍스트 레이어 정상          → ODL + bbox 재구성   (지금 것)
Tier 1  NATIVE_SUSPECT  레이어는 있으나 깨짐        → OCR 재추출 후 비교
Tier 1  SCAN_SIMPLE     텍스트 없음 · 단순 레이아웃  → PP-StructureV3 (OCR)
Tier 2  SCAN_COMPLEX    텍스트 없음 · 표/다단/회전   → PaddleOCR-VL
```

**"OCR vs VL" 이 아니라 "네이티브 vs OCR vs VL"** 이다. VL 은 OCR 의 대체가 아니라
OCR 이 못 푸는 페이지의 마지막 폴백이다. 실측이 이걸 뒷받침한다 —
같은 이미지에서 [OCR 이 이긴 것](parser-vl-eval.md)과 VL 이 이긴 것이 갈렸다.

## M1 실측 (2026-09-08, data/input 3개 KB 약관 1,663페이지)

| 판정 | 페이지 | 비중 | 경로 |
|---|---|---|---|
| NATIVE | 1,575 | **94.7%** | ODL |
| NATIVE_SUSPECT | 0 | 0.0% | — |
| SCAN_SIMPLE | 20 | 1.2% | OCR |
| SCAN_COMPLEX | 68 | 4.1% | VL |

### 임계치가 데이터로 검증됐다

감으로 정한 값이 실제 분포에서 두 무리 사이에 정확히 앉았다:

```
NATIVE        char_count min =  66   ┐  50~66 이 비어 있다
                                     ├→ 임계 50 이 정확히 그 사이
SCAN_COMPLEX  char_count max =  46   ┘
SCAN_SIMPLE   char_count max =  28

image_cover   max = 0.756  < 임계 0.80  → 진짜 스캔 페이지 0개
garbage_ratio max = 0.012  < 임계 0.15  → 깨진 텍스트 레이어 0개 (임계의 1/12)
```

`garbage_ratio` 가 0 이라는 건 한글 CID 폰트 문제가 이 코퍼스엔 없다는 뜻이다.
다른 회사 약관이 들어오면 달라질 수 있어 신호는 남겨둔다.

### 예산 — 이 설계의 값어치

```
전량 VL      1,663p × 72.5s = 33.5시간
캐스케이드      68p × 11s    = 12.5분     ← 160배 절감
```

## 설계 원칙

**분류를 LLM/VLM 에 맡기지 않는다.** 98% 정확도 분류기도 1,000건이면 20건을 엉뚱한 경로로
보낸다. 규칙은 틀리는 방식이 예측 가능해 디버깅이 되고 페이지당 수 ms 로 끝난다.
이 프로젝트의 precision-first 와 같은 규율이다.

**깨진 레이어를 먼저 본다.** `classify()` 가 `garbage_ratio` 를 `char_count` 보다 **먼저**
검사하는 이유 — char_count 로 먼저 통과시키면 깨진 텍스트가 NATIVE 로 새어 조용히
쓰레기를 색인한다. 한글 PDF 의 흔한 실패는 "텍스트가 없다"가 아니라 "있는데 깨졌다"다.

**혼합 페이지**(네이티브 텍스트 + 이미지 표)는 페이지 전체를 VL 로 보내지 않는다.
텍스트는 ODL 로, 이미지 영역만 crop 해서 VL 에 넣고 bbox 위치에 끼운다.
공통 스키마가 block 단위라 가능해진다(M2).

## 다음 단계

| # | 할 일 | GPU |
|---|---|---|
| **M1** | ✅ triage + 분포 리포트 (`make triage`) | 불필요 |
| M2 | 공통 스키마 — ODL/OCR/VL 출력을 `Block` 으로 통일, bbox 재구성이 Block 을 먹게 | 불필요 |
| M3 | 합성 스캔셋(네이티브 PDF 를 래스터라이즈) + CER 측정 → OCR 경로 기준선 | GPU |
| M4 | VL 폴백 + Celery `gpu` 큐(concurrency=1, 문서 단위 배치) | GPU |
| M5 | 품질 게이트 + 격상(OCR confidence 낮으면 VL 로 1회) | GPU |

M2 가 가장 값어치 있다 — **bbox 재구성이 읽기순서를 실제로 고치는 유일한 도구**인데
(ODL·하이브리드·VL 셋 다 실패, [실측](parser-vl-eval.md)), 지금은 ODL 출력에만 먹는다.
공통 스키마로 묶으면 OCR/VL 결과에도 그대로 적용된다.

**하지 말 것**: VL 전량 처리 · 분류를 LLM 에 맡기기 · 원본 PDF 없는 골든으로 회귀 판정 ·
분포 보기 전에 임계치 정하기.
