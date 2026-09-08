# 재부팅 후 스택이 안 뜨는 문제 — 원인과 대책

컴퓨터를 켤 때마다 docs-rag 가 정상으로 안 올라오던 문제. 2026-09-07·09-08 이틀 연속
실측해 원인을 규명하고 자동화했다.

## 결론 (먼저)

```bash
make up          # 이거 하나. 기동 + 검증 + 자가복구
```

부팅 시 **자동 실행**되도록 systemd 유저 유닛도 설치돼 있다(설치법은 맨 아래).
로그: `~/.local/state/docs-rag-boot.log`

---

## 원인 1 — Docker Desktop 의 WSL bind 마운트 캐시가 무효화된다 (주범)

Docker Desktop 은 **Windows 쪽 프로세스**고 우리 프로젝트는 Ubuntu(WSL2) 안에 있다.
그래서 `./data:/data` 같은 bind 마운트를 배포판 경계 너머로 프록시한다:

```
/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/<해시>
```

이 경로를 **캐시**하는데, WSL 이 재부팅하거나 호스트 디렉토리가 지워졌다 다시 생기면
캐시가 무효가 된다. 그 결과가 두 갈래로 갈린다:

| 증상 | 겉보기 | 위험 |
|---|---|---|
| ① **빈 디렉토리로 마운트** | 컨테이너 `running`, 파일만 안 보임 | **조용함 — 이게 위험** |
| ② 마운트 자체 실패 | `no such file or directory` 로 컨테이너 생성 실패 | 시끄러움 |

①이 무서운 이유: `docker compose ps` 는 전부 `running` 이라 정상으로 보인다. ODL 은
"파일 없음"으로 status 91 을 내고, 사람이 로그를 열기 전엔 모른다.

**실측(2026-09-07 부팅 직후)** — 마운트 설정은 `docker inspect` 로 봐도 완전히 동일한데
결과가 갈렸다. 차이는 **뜬 순서 6초**뿐이었다:

```
odl · paddle    시작 00:24:10 → /data 빈 디렉토리   ❌
api · celery    시작 00:24:16 → 정상                ✅
```

배포판 간 파일시스템이 준비되기 전에 시작된 컨테이너가 빈 경로를 bind 해버린다.
전형적인 부팅 레이스다. 다음 날(09-08)에도 odl·paddle 이 똑같이 깨졌다 — 재현된다.

**복구**: `docker compose up -d --force-recreate <서비스>` → 마운트를 다시 해소한다.

## 원인 2 — restart 정책은 depends_on 을 지키지 않는다

WSL 이 재부팅하면 Docker 데몬이 `restart: unless-stopped` 컨테이너를 **각각 독립적으로**
되살린다. compose 의 의존 그래프를 다시 읽지 않는다 — 그건 compose CLI 의 개념이지
데몬의 개념이 아니다.

```
depends_on      →  `docker compose up` 이 오케스트레이션할 때만 유효
restart 정책     →  데몬이 부팅 후 복구할 때. 의존 무시
```

**실측**: postgres·qdrant 가 `unless-stopped` 인데도 안 올라왔고(exit 127, RestartCount=0),
api 는 **DB 없이 혼자 떠 있었다.** depends_on 으로는 이걸 못 막는다.

## 원인 3 — vllm 의 hard depends_on 이 스택 전체를 끌고 내려갔다

vllm 컨테이너 **시작**이 실패하면(모델 없음 · 위 마운트 캐시 무효화)
`docker compose up -d` 가 거기서 중단되고, depends_on 으로 매달린 api·celery·flower 가
`Created` 로 방치된다.

→ **api·celery 의 depends_on 에서 vllm 을 뺐다.** LLM 은 요청 시점 HTTP 호출이라 기동
의존이 아니다. vLLM 이 없어도 SQL 결정론 경로·`/retrieve`·거절 게이트는 전부 정상 동작한다.
(`vllm: {required: false}` 로도 시도했으나 **효과 없었다** — 3방식 실측: required:true →
api `created` ❌ / required:false → `created` ❌ / depends_on 제거 → `running` ✅.)

기동 순서를 잡는 책임은 `stack_up.sh` 로 옮겼다. vllm 은 서빙까지 **2분 38초** 걸리므로
(weights 10.9s + init engine 75.9s) 앱보다 **먼저** 띄우는 게 맞다.

---

## 대책 — `make up` 이 하는 일

1. **docker 데몬 대기** (최대 `DOCKER_WAIT_SEC`, 기본 120s)
   Docker Desktop 은 Windows 쪽이라 WSL 부팅보다 늦다. 부팅 자동실행 시 필수.
2. **순서 기동**: 인프라 → vllm(느리니 먼저, 모델 없으면 건너뜀) → 앱
3. **마운트 검증** — `docker inspect` 를 믿지 않고 **호스트 문서 수와 대조**한다.
   설정이 아니라 *실제로 보이는 것*을 본다. 원인 1의 ①을 잡는 유일한 방법.
4. **자가복구** — 깨진 서비스를 자동 `--force-recreate` 하고 **재검증**한다.
   안내만 하면 매 재부팅마다 사람이 개입해야 하고, 안 보면 조용히 깨진 채 돈다.
5. **응답 확인** — postgres `pg_isready`, qdrant/odl/api HTTP.

```bash
make up                              # 평소
DOCKER_WAIT_SEC=300 make up          # 부팅 직후처럼 도커가 늦을 때
STACK_UP_FORCE_BROKEN=odl make up    # 자가복구 경로 자체를 검증
```

## 부팅 자동 실행 (설치돼 있음)

```bash
systemctl --user status docs-rag     # 상태
systemctl --user start docs-rag      # 수동 실행
tail -f ~/.local/state/docs-rag-boot.log
```

유닛은 `~/.config/systemd/user/docs-rag.service`. `loginctl enable-linger` 로 로그인
없이도 부팅 시 뜬다. Docker Desktop 준비가 늦으면 `Restart=on-failure` 로 30초 뒤
재시도(최대 5회).

재설치가 필요하면:
```bash
systemctl --user daemon-reload && systemctl --user enable --now docs-rag
loginctl enable-linger "$USER"
```

## 그래도 안 될 때

```bash
docker compose down && make up       # 마운트 캐시를 통째로 다시 만든다
```
Docker Desktop 자체를 재시작하는 게 가장 확실하다 — 캐시가 거기 있기 때문이다.

---

# PP-StructureV3(OCR)가 추론 시 죽는다 — paddle 3.3.1 oneDNN/PIR 버그

**증상**: `/ocr` 요청이 HTTP 000(연결 끊김)으로 끝나고 컨테이너 RestartCount 가 증가한다.
메모리는 문제가 아니다(6g 로 올려도 901MiB 만 씀). GPU 여유와도 무관(vLLM 내려도 동일).

**원인** — 컨테이너 안에서 직접 실행해 잡았다(HTTP 경유로는 재시작에 에러가 묻힌다):

```
NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute
  not support [pir::ArrayAttribute<pir::DoubleAttribute>]
  at .../new_executor/instruction/onednn/onednn_instruction.cc:116
```

레이아웃 검출 모델을 oneDNN 경로로 실행할 때 PIR 신 실행기가 속성 타입을 못 바꾼다.
`paddle 3.3.1` + `paddleocr 3.4.0` + `paddlex[ocr] 3.4.3` 조합의 버그다.

**안 통한 것들** (전부 실측):
- `paddle.set_flags({"FLAGS_use_mkldnn": False})` — server.py 가 이미 하고 있으나 **안 먹는다**
- 환경변수 `FLAGS_use_mkldnn=0`, `FLAGS_enable_pir_api=0`, 둘 다 — 동일 에러
- `mem_limit` 3g→6g — 무관(사용량 901MiB)
- vLLM 내려 GPU 확보 — 무관

paddlex 가 **자체 Config 로 predictor 를 만들기** 때문에 paddle 전역 FLAGS 가 안 닿는다
(`paddlex/inference/models/common/static_infer.py`). `PPStructureV3.__init__` 에도
mkldnn/device 파라미터가 없다.

**영향**: `ocr.py` 가 실패를 빈 결과로 대체해 **image/table 청크가 조용히 사라진다.**
실측 코퍼스: text 7,882 · table 851 · **image 0**. 이미지 451개(19개 문서)를 뽑아놓고
하나도 못 쓰고 있었다. docling 하이브리드가 같은 패턴으로 한 번도 안 돌던 전례와 동일하다.

**대응**: 실패를 WARNING → ERROR 로 올려 관측에 노출했다. 근본 해결은 버전 조합 변경
(paddle/paddleocr 다운그레이드 또는 업그레이드)이 필요한데, **파싱을 PaddleOCR-VL 로
옮기는 방향과 겹치므로** 그쪽 실측 결과를 보고 정한다.

확인 명령:
```bash
docker compose exec paddle python3 -c "
import paddle; paddle.set_device('cpu')
from paddleocr import PPStructureV3
e=PPStructureV3(use_doc_orientation_classify=False, use_doc_unwarping=False, lang='korean')
print(len(list(e.predict('/data/output/raw/<문서>_images/<이미지>.png'))))"
```

---

# paddle 컨테이너가 GPU 를 못 본다 — CUDA compat 라이브러리가 WSL 드라이버를 가린다

**증상**: `nvidia-smi -L` 은 컨테이너 안에서 정상 동작(GPU 0 보임)인데
`paddle.device.cuda.device_count()` 가 **0**, `get_device_capability()` 는
`CUDA error(100) no CUDA-capable device is detected`.

같은 GPU·같은 호스트에서 vllm 컨테이너는 정상이다. 차이는 **LD_LIBRARY_PATH** 였다:

```
paddle  /usr/local/cuda-13.0/compat : ... : /usr/local/cuda/lib64
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^ 여기 libcuda.so.580.82.07 (96MB) 가 있다
vllm    /usr/local/nvidia/lib64 : /usr/local/cuda/lib64
        (compat 없음 → WSL 드라이버 /usr/lib/wsl/lib 를 정상적으로 찾는다)
```

호스트 WSL 드라이버는 **610.74** 인데 compat 라이브러리는 **580** 대응이다.
더 낮은 버전이 앞에 있어 실제 드라이버를 가리고, CUDA 초기화가 "장치 없음" 으로 실패한다.
(`/usr/lib/x86_64-linux-gnu/libcuda.so.1` 187KB 는 두 컨테이너 모두 갖고 있는 **스텁**이라
원인이 아니다 — vllm 도 같은 스텁을 갖고 정상 작동한다.)

**해결** — WSL 드라이버 경로를 앞에 둔다:
```bash
docker compose exec -e LD_LIBRARY_PATH=/usr/lib/wsl/lib:/usr/local/cuda-13.0/targets/x86_64-linux/lib \
  -e CUDA_VISIBLE_DEVICES=0 paddle python3 -c "
import paddle; print(paddle.device.cuda.device_count(), paddle.device.cuda.get_device_capability())"
# → 1 (8, 9)   ← Ada sm_89 정상 인식
```

**참고**: compose 의 paddle 주석은 *"RTX PRO 6000 Blackwell(sm_120) 미지원으로 CPU 고정"*
이라고 돼 있는데 **이 장비에는 해당 없다**(RTX 4060 Laptop = Ada sm_89). 다른 환경에서
가져온 주석이다. 이 머신에서는 위 경로만 잡으면 GPU 가 동작한다.
