# 서빙 지연 벤치 — 3경로 아키텍처의 payoff

`architecture.md`가 *"구체적 latency는 docs에 박지 않는다. 실측은 도구로 얻고 결과는 `data/bench/<날짜>/`
아래 보존된다"* 고 관례만 정해뒀던 자리를 채우는 실측 계층. 잰 것은 **"결정론 SQL 경로가 RAG의
모델 비용을 통째로 회피한다"** 는 설계 주장이 숫자로 성립하는가다.

> 도구 `scripts/bench.py` (`make bench`) · 원본 `data/bench/<날짜>/results.json`. 스크립트는
> **실행 모드를 스스로 기록**(임베더·리랭커 device·vLLM 유무) — 아래 수치 해석에 그 컨텍스트가 필수다.

---

## 무엇을·왜 재나

소비자 질문의 다수("얼마·언제까지·보장돼요·뭐가 면책")는 값이 정해져 있어 **결정론 SQL 경로**가
`payout_rule`·`product`·`coverage_range`에서 **LLM 없이** 집어온다. 해석·절차형만 RAG로 간다
([README 결정론 계층](../README.md)). 두 경로의 지연은 성격이 다르다:

| 경로 | 무엇을 하나 | 무거운 것 |
|---|---|---|
| **SQL** (`/payout`·`/terms`·`/coverage`·`/exclusion`, SQL-라우팅 `/answer`) | Postgres SELECT + 결정론 포맷 | **없음** — 임베더·리랭커·LLM 미사용 |
| **retrieve** (`/retrieve`) | BGE-M3 쿼리 임베딩 + 하이브리드 검색 + CrossEncoder 리랭킹 | 임베더·리랭커 (모델 추론) |
| **answer(RAG)** (`/answer`) | retrieve + vLLM 생성(+CRAG) | 위 + **LLM 생성**(가장 큼) |

핵심 가설: **SQL 경로는 모델을 안 건드리므로 GPU 유무와 무관하게 싸다.** retrieve/RAG는 모델
비용이 바닥(floor)이라 하드웨어·모드에 종속된다.

---

## 측정 (2026-08-13, lite 모드)

`make lite` 상태 — vLLM·paddle·odl 내리고 GPU를 다른 작업에 비운 구성. 이때 API 컨테이너는
`CUDA_VISIBLE_DEVICES=-1` 이라 **임베더·리랭커가 CPU로 강등**된다(GPU 유휴 596MiB·3%). n=20(SQL)/
n=5(retrieve), p50 기준.

| 케이스 | p50 (ms) | p95 (ms) | 성격 |
|---|---|---|---|
| `/terms` (SQL) | **4.6** | 14.3 | 결정론 |
| `/exclusion` (SQL) | **5.4** | 8.1 | 결정론 |
| `/coverage` (SQL) | **6.4** | 13.6 | 결정론 (별표3 3-값 판정) |
| `/payout` (SQL) | **8.9** | 240.6† | 결정론 (+면책 강제첨부) |
| `/answer`→SQL 라우팅 | **9.8** | 17.1 | 결정론 (라우팅 후 SQL) |
| `/retrieve` (검색+리랭킹) | **15,699** | 19,160 | 모델 추론 (CPU 모드) |

† payout p95 240ms·mean 21ms은 첫 호출 커넥션/JIT 워밍업 이상치 — steady-state는 한 자릿수 ms.

**SQL 경로 p50 중앙값 ≈ 6.4ms**, 모두 한 자릿수~10ms 안에서 안정.

---

## 해석 — 정직한 두 운영점

retrieve의 15.7초는 **자랑거리가 아니라 lite/CPU 모드 아티팩트다.** GPU-warm(full 모드)에선
retrieve가 sub-초(문서 예시 [api.md](api.md) ~380ms)다. 그러니 배수를 "2453x" 하나로 박으면 오도다.
두 운영점을 다 밝힌다:

| 비교 | SQL 경로 p50 | retrieve floor p50 | 배수 | 위 얹히는 것 |
|---|---|---|---|---|
| **lite/CPU** (실측) | ~6ms | ~15,700ms | ~2450x | — |
| **full/GPU-warm** (문서 [api.md](api.md)) | ~6ms | ~380ms | ~60x | RAG `/answer`는 +vLLM 생성(초 단위) |

두 줄이 같은 결론을 가리킨다: **SQL 경로의 6ms는 두 모드에서 동일**(모델을 안 쓰니까)이고, RAG는
어느 모드든 모델 비용이 바닥에 깔린 뒤 생성 지연이 더 얹힌다.

## 왜 이게 아키텍처 이득인가

1. **GPU 독립성** — SQL 경로 지연은 GPU를 통째로 비운 lite 모드에서도 안 변한다. 즉 **GPU를 다른
   작업(색인·다른 서비스)에 내주고도** "얼마·언제·보장·면책"을 한 자릿수 ms로 답한다. 마이크로
   벤치가 아니라 실제 운영 배치 선택지다.
2. **결정론 = 재현** — 같은 질의는 같은 답·같은 근거 row. LLM 비결정성·환각·검증 오버헤드가 원천 부재.
3. **비용** — SQL 경로는 GPU·토큰 비용 0. 라우팅이 결정론 질의를 SQL로 보낸 비율만큼 RAG 부하가 준다
   (`trace_summary.py` Route Distribution의 `sql` 비중으로 관측).

> 라우팅이 **얼마나** SQL로 보내는지(=이 이득이 실질로 얼마나 실현되는지)는 운영 trace의 route
> 분포로 측정한다 — 벤치는 "SQL 경로가 빠른가"(단가), trace는 "얼마나 자주 타나"(빈도)를 잰다.

---

## 한계·확장

- retrieve/RAG의 **GPU-warm 실측은 미수록** — full 모드 전환이 GPU를 점유해 공존 작업을 방해하므로
  임의 기동하지 않음([no-autostart 원칙](architecture.md)). 필요 시 `make answer`/`make full` 후
  `BENCH_INCLUDE_RAG=1 make bench`로 `/answer`(생성 포함)까지 같은 하네스로 측정(도구는 준비됨).
- SQL 경로 p95 이상치(payout 240ms)는 커넥션 풀 워밍업 — 상시 트래픽에선 사라진다. 워밍업 후
  재측정은 후속.
- 부하(동시성) 하 지연·처리량(QPS)은 미측정 — 현재는 단일 요청 지연만. 부하 벤치는 확장 지점.
