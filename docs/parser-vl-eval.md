# PaddleOCR-VL vs ODL — 파싱 전환 실측 (2026-09-08)

**질문**: ODL 이 못 잡는 **다단 읽기순서**를 VL 파서가 고쳐주나? 파싱을 VL 로 옮길 값어치가 있나?

## 결론: **지금은 아니다.** 순서 개선 없이 260배 느리다.

| 축 | ODL (java, CPU) | PaddleOCR-VL (GPU) | 판정 |
|---|---|---|---|
| 영어 2단 합성 PDF 읽기순서 | ❌ 틀림 | ❌ **다르게 틀림** | 개선 없음 |
| 실제 KB 2단 4p — 조 언급/고유 조 | 17 / 11 | 17 / 11 | 동일 |
| 실제 KB — **조 오름차순 비율** | 0.75 | 0.75 | 동일 |
| 실제 KB — heading 수 | 12 | 13 | VL 근소 우세 |
| **처리 속도** | 0.3s/페이지 | **72.5s/페이지** | **260배 느림** |

1,035페이지 KB 약관 환산: ODL **4.8분** vs VL **20.8시간**.

### 읽기순서 상세 (정답: A3 → A3본문 → A4 → A4본문 → A5 → A5본문 → A6 → A6본문)

```
ODL(java)      A3 A3본문 A4 A5 A5본문 A6 A4본문 A6본문      ❌
ODL(hybrid)    A3 A5 A3본문 A4 A4본문 A5본문 A6 A6본문      ❌
PaddleOCR-VL   A3 A3본문 A4본문 A5 A4 A5본문 A6 A6본문      ❌
bbox 재구성     A3 A3본문 A4 A4본문 A5 A5본문 A6 A6본문      ✅
```

**이미 갖고 있는 `reconstruct_reading_order.py`(bbox 기반)가 셋 다 이긴다.**
JSON 레이아웃 트리의 `bounding box` 로 `page → 단 → y` 재정렬하는 그 코드다.

## 그래서 파싱 전환은 보류

- **개선이 없다** — 다단 순서가 안 고쳐지고 구조 지표도 동일하다
- **비용이 크다** — 260배 느리고 GPU 를 점유한다(vLLM 과 순차 사용 필요)
- **이미 더 나은 도구가 있다** — bbox 재구성이 순서를 실제로 복원한다

단 프로젝트 문서에 이미 적혀 있듯 **KB 의 진짜 병목은 순서가 아니라 semantic 구분**
("1237p 안 진짜 특약 vs 요약/참조/중복")이다. 읽기순서는 이미 0.95~0.97 수준이고
`reconstruct_reading_order.py` 독스트링이 그 한계를 정확히 기록해 뒀다.

## 재현 방법

VL 은 GPU 가 필요하고 paddle 컨테이너는 기본 CPU 고정이라 오버레이가 필요하다.
CUDA compat 문제도 함께 우회해야 한다(troubleshooting-boot.md 참조):

```bash
docker compose stop vllm            # GPU 확보 — 동시 상주 불가(8GB)
docker compose exec -T \
  -e LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/local/cuda-13.0/targets/x86_64-linux/lib \
  -e CUDA_VISIBLE_DEVICES=0 paddle python3 -c "
import paddle; paddle.set_device('gpu:0')
from paddleocr import PaddleOCRVL
p = PaddleOCRVL()                    # 인자 없이 — paddlex 가 자체 포맷으로 받는다
for r in p.predict('/data/input/<파일>.pdf'):
    r.save_to_markdown('/data/input/out')"

python3 scripts/compare_parsers.py --md-a <odl>.md --md-b <vl>.md
```

주의: HuggingFace 포맷(`PaddlePaddle/PaddleOCR-VL`)을 `vl_rec_model_dir` 로 주면
"Model name mismatch" 로 거절한다. paddlex 는 자체 inference 포맷을 쓴다.
받아지는 실제 모델은 `PaddleOCR-VL-1.5` + `PP-DocLayoutV3` 이고 paddle_cache 볼륨에 남는다.

## 다시 볼 조건

- 스캔 PDF(텍스트 레이어 없음)를 다루게 되면 — 지금 코퍼스는 전부 텍스트 레이어가 있다
- VL 모델이 크게 빨라지거나 GPU 예산이 늘면
- semantic 세그먼테이션(진짜 병목)을 VL 이 도와줄 수 있다는 근거가 생기면
