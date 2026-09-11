# 청킹 전략

## 전략 개요

`.env`의 `CHUNKER_TYPE`으로 전략 선택. 같은 DB/인덱스에 공존 가능.

| | Adaptive | Fixed |
|---|---|---|
| 목적 | 프로덕션 | A/B 비교 실험용 |
| 분할 기준 | 헤딩 트리 + Text/Table 분리 | 800자 윈도우 슬라이딩 |
| chunk_type | `text` / `table` / `image` | 미사용 |
| part_index/part_total | O (sibling 복원용) | 미사용 |
| 임베딩 텍스트 | `heading_path + content` | `content`만 |

## 크기 파라미터

BGE-M3 sweet spot: 256~512 tokens ≈ 800~1600 chars (한국어 기준)

| 설정 | 값 | 근거 |
|------|-----|------|
| TEXT_MAX_CHARS | 1200자 | sweet spot 중앙 (~400 tokens) |
| TABLE_MAX_CHARS | 2400자 | 마크다운 오버헤드 감안 시 실질 ~400 tokens |
| CHUNK_MIN_CHARS | 300자 | 자투리 병합 기준 |
| FIXED_WINDOW_SIZE | 800자 | sweet spot 하단 |
| FIXED_OVERLAP_SIZE | 150자 | 경계 문맥 보존 |

경험적 설정값. 실제 검색 품질을 보면서 조정.

## Adaptive 상세

### Text / Table / Iqmage 분리

| | 텍스트 | 테이블 | 이미지 OCR |
|---|---|---|---|
| 분할 기준 | TEXT_MAX_CHARS 초과 시 문장 경계 분할 | TABLE_MAX_CHARS 초과 시 행 분할 | OCR 결과 전체를 하나의 청크 |
| 자투리 병합 | CHUNK_MIN_CHARS 미만 → 이전 청크에 병합 | — | — |
| 행 분할 시 | — | 헤더(컬럼명 + 구분선) 매 청크 반복 | — |
| chunk_type | `text` | `table` | `image` |

### 표 캡션 — 표 청크에 '무슨 조건의 값인지'를 붙인다

표 청크에는 **값만 있고 조건이 없다.** 실측(R05 제3조 보험금의 지급사유): 같은 모양의 지급표가
세 번 나오는데 하나는 *상해를 원인으로*, 하나는 *질병을 원인으로*, 하나는 *재가입계약의 경우*
이고 값이 다르다(레진 충전치료 50% vs 0·25·50%). 그런데 그 조건 라벨은 표 **바로 앞 줄**에
있어서 다른 청크로 떨어지고, heading_path 는 셋 다 `제3조(보험금의 지급사유)` 로 같다.
→ 검색이 표 하나만 회수하면 어느 조건의 값인지 **알 방법이 없다.** 검색도 LLM 도.

그래서 표 바로 앞 줄이 캡션이면 표 청크 본문 맨 앞에 붙인다(`_table_caption`).

**왜 heading_path 가 아니라 content 인가**
- 임베딩·리랭커가 그대로 먹는다(둘 다 `heading_path + content` 를 본다)
- LLM 컨텍스트에 조건이 들어간다 — 표만 인용해도 답이 조건을 말할 수 있다
- 본문이 달라지므로 상해표와 질병표가 **중복으로 안 묶인다**(실측 R05 중복 29군·97청크 → 24군·72청크)
- heading_path 를 건드리면 `part_index`/`part_total` 과 sibling 복원의 그룹 키가 흔들린다

**임계 30자는 분포가 아니라 정밀도 절벽에서 왔다** (표 청크 851, 앞줄 있는 것 683 실측):

| 앞줄 길이 | 건수 | 무엇이 잡히나 |
|---|---|---|
| ~30자 | 240 | 전부 진짜 캡션 — `질병을 원인으로 …`, `<최초계약의 경우>`, `구분 기간 지급이자` |
| 31~45자 | 6 | 반반 — `가. 의료급여기관 및 …범위`(캡션) vs `4.1 2019.9.1 2019.10.1`(표 데이터) |
| 46~60자 | 3 | 전부 본문 조각 — `2 제1항에서 정하지 않은 용어의 뜻은 …` |

9건을 포기하고 오염을 0으로 두는 쪽을 골랐다 — **잘못 붙은 캡션은 조건을 거짓으로 말한다.**
문장 종결(`…다/요/음/함`)로 끝나면 캡션이 아니고, `&lt;최초계약의 경우&gt;` 는 `html.unescape`
로 푼다(안 풀면 조건 라벨이 엔티티 문자열로 임베딩돼 신호가 죽는다).

**캡션만 있는 텍스트 청크는 따로 내보내지 않는다** — 내용은 표에 실려 살아 있고, 20자짜리
고아 청크는 검색 잡음이다(실측 R05 에 26개 있었다).

**주의**: 캡션은 `_assign_page_ranges` **뒤에** 붙인다. 그 함수는 원문에서 본문을 찾아 페이지를
매기는데, 원문에 없는 캡션(마커 제거·엔티티 해제됨)을 미리 붙이면 find 가 실패해 그 청크부터
페이지 귀속이 노드 단위로 뭉개진다.

### chunk_type 설계 원칙

**출처가 아니라 내용의 성격이 기준**이다. ODL markdown에서 파싱된 표든 paddle OCR로 이미지에서 복원한 표든, 행·열 구조가 있으면 `chunk_type="table"`. 평문 텍스트는 `chunk_type="text"`(markdown 본문) 또는 `chunk_type="image"`(이미지 OCR). LLM과 검색 레이어는 출처 구분 없이 동일하게 취급한다.

**분리 저장 + 메타데이터 연결**: 같은 이미지에서 나온 image/table 청크는 동일한 `heading_path`를 공유한다. `expand_siblings()` ([src/v1/rag/sibling.py](../src/v1/rag/sibling.py))가 heading_path 기준으로 청크를 묶어 가져오므로, 검색에서 table 청크가 hit되면 같은 조항의 image 청크(주변 설명)가 함께 LLM context에 들어간다. 합쳐서 한 청크로 만들면 표 구조가 평문에 섞여 LLM이 표로 인식 못 하므로 비표준.

### 이미지 OCR 파이프라인 (extract → ocr → chunk)

```
[extract] ODL이 PDF를 markdown으로 변환 + 내부 이미지 객체를 파일로 떨굼
          (image_output="external": 1px spacer, 투명 오버레이 등 garbage 포함)
          ↓ celery extract.py 후처리 (_prune_garbage_images)
          is_valid_image 6단계 필터로 garbage PNG 목록 선별 → ODL /cleanup API로 일괄 삭제
          (UID 1000 소유라 워커가 직접 rm 불가 → ODL 컨테이너의 cleanup API 경유)

[ocr]     celery가 markdown의 ![image N](...) 태그 파싱
          │
          ├── [1] is_valid_image 6단 입구 필터 (ODL object-level garbage filter)
          │   · 파일 크기     file_size < 100B
          │   · 최소 차원      dimension  < 10px
          │   · figure 최소    short side < 300x200 (아이콘·로고·QR)
          │   · 종횡비 상한    aspect     ≥ 10:1 (가로띠)
          │   · 최대 차원      dimension  > 7000px (대형 배너)
          │   · 단색 검출      stddev     < 5 (단색·투명 마스크)
          │   — garbage 이미지는 여기서 컷, paddle 호출 안 함
          │
          ├── [2] paddle HTTP 호출 → PP-StructureV3 처리 (개별 이미지 파일만)
          │
          ├── [3] _extract_blocks 라벨 분류
          │   drop: header/footer/page_number/... → 청크 제외
          │   table: → chunk_type="table" 개별 청크
          │   나머지: text/title/caption/formula/... → chunk_type="image" 합친 청크
          │
          └── [4] is_meaningful_ocr_result + heading 중복(Jaccard>0.7) → 최종 드롭

[chunk]   markdown text 청크 + OCR 청크(image + table) 합류 → part_index 재부여
```

`paddle`은 이미지 파일만 처리하며 PDF를 직접 다루지 않는다.

### 필터 임계값 (config/settings.py)

| 상수 | 값 | 역할 |
|---|---|---|
| `OCR_MIN_FILE_SIZE` | 100 | 파일 크기 필터 — 빈 PNG 차단 |
| `OCR_MIN_IMAGE_WIDTH/HEIGHT` | 10 | 최소 차원 필터 — 1~2px spacer 차단 |
| `OCR_FIGURE_MIN_WIDTH/HEIGHT` | 300/200 | figure 최소 크기 필터 — 아이콘·로고·QR 차단 |
| `OCR_MAX_ASPECT_RATIO` | 10 | 종횡비 필터 — 페이지 구분선·가로띠 차단 |
| `OCR_MAX_IMAGE_DIMENSION` | 7000 | 최대 차원 필터 — 대형 배너 차단 (리사이즈 오버헤드 방지) |
| `OCR_MIN_PIXEL_STDDEV` | 5.0 | 단색 필터 — 빈 배경·투명 마스크 차단 |
| `OCR_MIN_TEXT_LENGTH` | 10 | 출구 필터 — 너무 짧은 OCR 결과 차단 |

### OCR 통계 로그

```
[OCR 통계] {doc} — 전체 N, 입구필터 M [file_size:x, icon_size:x, flat_color:x, ...],
           빈값 x, drop전용 x, 노이즈 x, 중복 x, 저장(image) x, 저장(table) x
```

- `drop전용`: header/footer만 있어서 콘텐츠 블록 0인 이미지
- `저장(image)`: 콘텐츠 텍스트 청크로 저장된 건수
- `저장(table)`: 표 청크 건수 (한 이미지에 여러 표 가능)

### 엔진 설정

`PPStructureV3(lang="korean", device="cpu", enable_mkldnn=False)` — CPU 모드 고정. Blackwell sm_120이 현재 `paddlepaddle/paddle:3.3.1-gpu-cuda13.0-cudnn9.13` 빌드에 미포함이라 GPU 초기화 불가. PIR+oneDNN 경로의 `NotImplementedError` 우회를 위해 `enable_mkldnn=False` 생성자 + `FLAGS_use_mkldnn=0` / `FLAGS_enable_pir_in_executor=0` env 3중 차단. 자세한 배경은 [architecture.md](architecture.md).

### 저장 산출물

각 이미지 옆에 `_ocr.json` + `_ocr_layout.png` 저장.

`_ocr.json` 스키마 (PP-StructureV3 결과):
```json
{
  "input_path": "...",
  "width": 1483, "height": 1082,
  "rec_texts": ["..."],           // REC_MIN_SCORE=0.5 이상만
  "rec_scores": [0.95, ...],      // REC_MIN_SCORE=0.5 이상만
  "layout_boxes": [{"label": "text", "score": 0.9, "bbox": [x1,y1,x2,y2]}],
  "parsing_blocks": [{"label": "...", "content": "..."}]
}
```

**저장 정책**: `is_valid_image` 입구 필터(1계층)를 통과해 paddle로 넘어온 이미지의 **파싱 결과 구조를 왜곡 없이** 저장하되, 저장 시점에 confidence 컷을 걸어 garbage 검출은 배제한다:

- **전처리 없음**: 이진화·블러 등 input 전처리 금지. PP-StructureV3 layout detector는 자연 RGB로 학습돼 이진화하면 layout_boxes가 10배 줄고 라벨이 뭉개짐 (실측 확인, 1 vs 10).
- **저장 컷**: `LAYOUT_MIN_SCORE=0.5` 이상인 layout_boxes만 JSON/PNG에 기록, `REC_MIN_SCORE=0.5` 이상인 rec_texts만 기록. 두 컷 모두 통과하는 게 하나도 없으면 `_ocr.json`/`_ocr_layout.png` 생성 자체를 스킵 (garbage 이미지로 디스크 낭비 방지).
- **의미 필터 위치**: drop/table/text 라벨 분류, heading 중복, 한글비율 체크는 전부 청킹 단계(3계층)에서. paddle 단계는 confidence 기반 물리적 컷만 담당.

`_ocr_layout.png`는 기본적으로 레이아웃 블록만 색상별 박스로 그리며, 환경변수 `PADDLE_VIZ_TEXT_LINES=1`이면 개별 텍스트 라인을 자홍색으로 추가 오버레이 (디버깅용).

필터 효과 검증: `scripts/eval_ocr.py`.

### Sibling 복원 (검색 시점)

같은 `heading_path` 내 `part_index`로 순서 추적:

```
예: "제4조" heading_path 내
  text   (part 1/4)  ← 검색 hit
  image  (part 2/4)  ← sibling 복원으로 같이 가져옴
  table  (part 3/4)  ← 같이 가져옴
  text   (part 4/4)  ← 같이 가져옴
```

hit된 part_index 기준 ±`SIBLING_WINDOW`(기본 2)개만 가져온다. 전체가 아닌 슬라이딩 윈도우. "검색은 작게, LLM 전달은 크게". chunk_type 무관, heading_path만으로 묶이므로 text/image/table이 자연스럽게 같은 섹션으로 복원된다.

### 이미지 OCR 청크 크기 리스크

BGE-M3는 8k 토큰까지 지원하지만, 512~1024 토큰 내에서 retrieval 품질이 가장 안정적이라는 보고가 있다. OCR 텍스트가 sweet spot(800~1600자)을 초과하면 임베딩 품질 저하 가능. 레이아웃 보존을 위해 분할하지 않는 보수적 전략이지만, 운영 데이터에서 이미지 청크 길이 분포를 주기적으로 확인하고 초과 비율이 높으면 bbox 기반 서브블록 분리 검토.

## Fixed 상세

- 800자 윈도우 + 150자 오버랩, 단어 중간 절단 방지
- TOC 제거 후 구조 무시하고 기계적 분할
- 각 윈도우에 가장 가까운 이전 헤딩을 메타데이터로 태깅 (content에는 미포함)

## JSON 출력 예시

### Adaptive — 텍스트
```json
{
  "service_code": "01",
  "source_file": "약관.pdf",
  "page_start": 15, "page_end": 15,
  "heading_path": ["제1장 일반사항", "제4조 보험금의 지급사유"],
  "heading": "제4조 보험금의 지급사유",
  "chunk_type": "text",
  "part_index": 1, "part_total": 4,
  "char_count": 142,
  "content": "회사는 보험기간 중 피보험자에게..."
}
```

### Adaptive — 이미지 OCR (콘텐츠 블록 합친 것)
```json
{
  "chunk_type": "image",
  "part_index": 2, "part_total": 4,
  "heading_path": ["제1장 일반사항", "제4조 보험금의 지급사유"],
  "image_paths": ["약관_images/img8.png"],
  "image_ocr_texts": ["보험금 지급사유\n1. 사망\n2. 후유장해\n..."],
  "char_count": 35,
  "content": "보험금 지급사유\n1. 사망\n2. 후유장해\n..."
}
```

### Adaptive — 테이블 (같은 이미지에서 분리됨)
```json
{
  "chunk_type": "table",
  "part_index": 3, "part_total": 4,
  "heading_path": ["제1장 일반사항", "제4조 보험금의 지급사유"],
  "image_paths": ["약관_images/img8.png"],
  "image_ocr_texts": ["| 구분 | 지급률 |\n| --- | --- |\n| 사망 | 100% |"],
  "char_count": 48,
  "content": "| 구분 | 지급률 |\n| --- | --- |\n| 사망 | 100% |"
}
```

> 같은 `image_paths`를 공유하는 image 청크와 table 청크는 동일한 `heading_path`로 묶여 sibling 복원에서 함께 LLM context에 들어간다. PP-StructureV3의 `table_res_list` HTML은 celery의 `_html_table_to_markdown()`이 마크다운 테이블로 변환.

### Fixed
```json
{
  "service_code": "01",
  "source_file": "약관.pdf",
  "page_start": 15, "page_end": 16,
  "heading_path": ["제1장 일반사항", "제4조 보험금의 지급사유"],
  "heading": "제4조 보험금의 지급사유",
  "char_count": 796,
  "content": "회사는 보험기간 중 피보험자에게..."
}
```

Fixed는 `chunk_type`, `part_index`, `part_total`, `image_paths` 없음.
