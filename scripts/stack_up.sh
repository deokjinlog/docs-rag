#!/usr/bin/env bash
# 스택 기동 + **검증**. `docker compose up -d` 를 대신한다 (make up).
#
# 왜 필요한가 — WSL2 + Docker Desktop 에서 재부팅 후 자동 복구가 신뢰할 수 없다.
# 실측(2026-09-07, WSL 부팅 09:23:49 직후):
#
#   postgres · qdrant   restart=unless-stopped 인데도 **안 올라옴** (exit 127, RestartCount=0)
#   odl · paddle        올라왔지만 `/data` bind 마운트가 **빈 디렉토리** (시작 00:24:10)
#   api · celery        정상 (시작 00:24:16 — 6초 늦게 떠서 살았다)
#   vllm                모델 없어서 14회 재시작 루프
#
# odl/paddle 과 api/celery 의 마운트 설정은 `docker inspect` 로 봐도 완전히 동일한데
# 결과가 갈렸다. 차이는 **뜬 순서 6초**뿐 — Docker Desktop 은 자기 WSL 배포판에서 돌고
# 우리 프로젝트는 Ubuntu 배포판에 있어서, 배포판 간 파일시스템이 준비되기 전에 시작된
# 컨테이너가 빈 경로를 bind 해버린다. 전형적인 부팅 레이스다.
#
# 무서운 건 **조용하다**는 것 — ODL 은 "파일 없음"으로 status 91 을 내고, 사람이 로그를
# 열어보기 전엔 모른다. 그래서 "올렸다"가 아니라 "올리고 확인했다"를 한 명령으로 만든다.
#
# 근본 메커니즘도 실측으로 잡혔다. Docker Desktop 은 WSL bind 마운트를
#   /run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/Ubuntu/<해시>
# 로 프록시하고 이 경로를 **캐시**한다. 호스트 디렉토리를 지웠다 다시 만들거나 WSL 이
# 재부팅하면 캐시가 무효가 되는데, 그 결과가 두 갈래로 나타난다:
#   ① 빈 디렉토리로 마운트됨      → 컨테이너는 running 인데 파일이 안 보임 (odl·paddle)
#   ② 마운트 자체 실패            → "no such file or directory" 로 컨테이너 생성 실패 (vllm)
# ②는 시끄럽지만 ①은 조용하다. 이 스크립트가 ①을 잡는 게 핵심이다.
# 복구는 해당 서비스 --force-recreate, 그래도 안 되면 docker compose down && make up.
set -uo pipefail
cd "$(dirname "$0")/.."

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; NC=$'\033[0m'
fail=0

say() { printf "  %-28s %s\n" "$1" "$2"; }

# ── docker 준비 대기 ──────────────────────────────────────────────────────────
# Docker Desktop 은 **Windows 쪽 프로세스**라 WSL 부팅보다 늦게 준비된다. 부팅 직후
# 자동 실행되는 경우(systemd 유닛) 여기서 기다리지 않으면 "daemon 없음"으로 헛돈다.
WAIT=${DOCKER_WAIT_SEC:-120}
for _ in $(seq 1 "$WAIT"); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  echo "${RED}❌ docker 데몬 응답 없음 (${WAIT}s 대기). Docker Desktop 이 떠 있는지 확인${NC}"
  exit 1
fi

HAS_LLM=0
[ -s model/Qwen3-4B-AWQ/config.json ] && HAS_LLM=1

# 순서: 인프라 → vllm(느림, 있으면) → 앱.
#
# vllm 을 **먼저** 띄우는 게 맞다 — 서빙까지 2분 38초 걸리므로 일찍 시작할수록 빨리
# 준비된다. 앱은 수 초면 뜨고 vLLM 없이도 SQL·검색·거절 경로가 다 동작하니 기다릴 이유가
# 없다. 즉 "vllm 이 먼저 뜨는 것"은 문제가 아니었고, 문제는 compose 의 hard 의존이었다
# (vllm 시작 실패 → up 전체 중단 → api·celery 가 Created 로 방치).
#
# 그래서 docker-compose.yml 의 api·celery depends_on 에서 **vllm 을 뺐다**. 대신 기동
# 순서를 잡는 책임이 이 스크립트로 왔다 — 여기서 명시적으로 vllm 을 앱보다 먼저 띄운다.
# 모델이 없으면 건너뛴다(재시작 루프로 CPU 를 태우지 않기 위해).
# ── 프로필 (D1) ──────────────────────────────────────────────────────────────
# 전 서비스를 한꺼번에 띄우면 mem_limit 합계가 WSL 15Gi 를 넘어 스택이 통째로 죽는다
# (실측 4회). 이제 서비스마다 프로필이 붙어 있고, 한 번에 뜨는 조합은 11Gi 이하다.
# 기본은 serve — 재부팅 후 자동 기동(systemd)이 노리는 상태가 '서빙 가능'이기 때문.
#   PROFILE=ingest bash scripts/stack_up.sh   처럼 바꿔 쓴다.
PROFILE="${PROFILE:-serve}"
IN_PROFILE=" $(docker compose --profile "$PROFILE" config --services 2>/dev/null | tr '\n' ' ') "
pick() { for s in "$@"; do case "$IN_PROFILE" in *" $s "*) printf '%s ' "$s";; esac; done; }
INFRA=$(pick postgres qdrant rabbitmq odl paddle)
APPS=$(pick api celery celery-ocr flower)

echo "▶ 스택 기동 (프로필: $PROFILE)"
[ -n "$INFRA" ] && docker compose up -d $INFRA >/dev/null 2>&1
case "$IN_PROFILE" in *" vllm "*) [ "$HAS_LLM" = 1 ] && docker compose up -d vllm >/dev/null 2>&1;; esac
[ -n "$APPS" ] && docker compose up -d $APPS >/dev/null 2>&1

echo "▶ 컨테이너 상태"
for svc in $INFRA $APPS; do
  st=$(docker compose ps "$svc" --format '{{.State}}' 2>/dev/null)
  if [ "$st" = "running" ]; then
    say "$svc" "${GRN}running${NC}"
  else
    say "$svc" "${RED}${st:-없음}${NC}"; fail=1
  fi
done

# vllm 은 모델 가중치가 있어야만 뜬다. 없으면 재시작 루프를 돌며 CPU 를 태우므로
# 실패로 세지 않되 **왜 안 뜨는지**는 정확히 알려준다.
case "$IN_PROFILE" in
  *" vllm "*) ;;
  *) HAS_LLM=-1 ;;              # 이 프로필엔 vllm 이 없다 — 진단도 하지 않는다
esac
if [ "$HAS_LLM" = -1 ]; then
  :
elif [ "$HAS_LLM" = 0 ]; then
  say "vllm" "${YLW}건너뜀 — model/Qwen3-4B-AWQ 비어 있음${NC}"
  echo "     └ 받기: uv run --no-project --with 'huggingface_hub[cli]' \\"
  echo "              hf download Qwen/Qwen3-4B-AWQ --local-dir model/Qwen3-4B-AWQ"
else
  st=$(docker compose ps vllm --format '{{.State}}' 2>/dev/null)
  say "vllm" "${st:-없음}"
fi

# ── 핵심: bind 마운트가 진짜 살아 있나 ────────────────────────────────────────
# 컨테이너가 running 이어도 마운트는 죽어 있을 수 있다(위 실측). 호스트에 보이는
# 문서 수와 컨테이너가 보는 수를 대조한다 — 설정이 아니라 **실제로 보이는 것**을 본다.
echo "▶ /data 마운트 검증 (호스트 대조)"
host_n=$(ls data/output/raw/*.md 2>/dev/null | wc -l | tr -d ' ')
say "host data/output/raw" "${host_n}개"

# 마운트가 깨졌으면 **직접 고친다**. 복구법은 이미 안다(--force-recreate) — 사람에게
# 명령을 안내만 하면 매 재부팅마다 사람이 개입해야 하고, 안 보면 조용히 깨진 채 돈다.
mount_count() {  # $1=서비스 $2=컨테이너내경로 → 보이는 문서 수
  # 검증용 훅: STACK_UP_FORCE_BROKEN 에 든 서비스는 0 을 돌려 자가복구 경로를 태운다.
  case " ${STACK_UP_FORCE_BROKEN:-} " in *" $1 "*) echo 0; return;; esac
  docker compose exec -T "$1" sh -c "ls $2/output/raw/*.md 2>/dev/null | wc -l" 2>/dev/null | tr -d ' \r'
}

# 깨진 서비스는 전역 BROKEN 에 담는다.
# ⚠ 함수 stdout 으로 반환하면 안 된다 — say() 진단 출력이 같은 stdout 이라 섞여서
# 결과가 항상 non-empty 가 된다(실측 버그). 진단은 사람이 봐야 하므로 stdout 이 맞고,
# 반환값은 전역으로 뺀다.
BROKEN=""
check_mounts() {
  BROKEN=""
  for pair in "odl:/data" "paddle:/data" "api:/app/data" "celery:/app/data" "celery-ocr:/app/data"; do
    local svc=${pair%%:*} path=${pair#*:}
    [ "$(docker compose ps "$svc" --format '{{.State}}' 2>/dev/null)" = "running" ] || continue
    local n; n=$(mount_count "$svc" "$path")
    if [ "${n:-0}" = "$host_n" ]; then
      say "$svc:$path" "${GRN}${n}개 ✓${NC}"
    else
      say "$svc:$path" "${YLW}${n:-?}개 ✗ (호스트 ${host_n}개와 불일치)${NC}"
      BROKEN="$BROKEN $svc"
    fi
  done
}

check_mounts
if [ -n "${BROKEN// /}" ]; then
  echo "  ↻ 자동 복구: docker compose up -d --force-recreate$BROKEN"
  # shellcheck disable=SC2086
  docker compose up -d --force-recreate $BROKEN >/dev/null 2>&1
  sleep 10
  echo "▶ /data 마운트 재검증"
  unset STACK_UP_FORCE_BROKEN     # 훅은 1회성 — 재검증은 진짜 상태를 본다
  check_mounts
  if [ -n "${BROKEN// /}" ]; then
    say "복구 실패" "${RED}${BROKEN}${NC}"
    echo "     └ 수동: docker compose down && make up"
    fail=1
  else
    say "자가복구" "${GRN}성공${NC}"
  fi
fi

# ── 의존 서비스가 실제로 응답하나 ────────────────────────────────────────────
echo "▶ 서비스 응답"
docker compose exec -T postgres pg_isready -U docsrag -d docsrag >/dev/null 2>&1 \
  && say "postgres" "${GRN}accepting${NC}" || { say "postgres" "${RED}응답 없음${NC}"; fail=1; }
curl -sf -m 5 http://localhost:6333/collections >/dev/null 2>&1 \
  && say "qdrant" "${GRN}응답${NC}" || { say "qdrant" "${RED}응답 없음${NC}"; fail=1; }
# 응답 검사도 프로필을 따른다 — 없는 서비스를 "응답 없음"으로 세면 정상 기동이 실패로 보인다
case "$IN_PROFILE" in *" odl "*)
  curl -sf -m 5 http://localhost:5002/health >/dev/null 2>&1 \
    && say "odl" "${GRN}응답${NC}" || { say "odl" "${YLW}health 무응답${NC}"; } ;;
esac
case "$IN_PROFILE" in *" paddle "*)
  curl -sf -m 5 http://localhost:5003/health >/dev/null 2>&1 \
    && say "paddle" "${GRN}응답${NC}" || { say "paddle" "${RED}응답 없음${NC}"; fail=1; } ;;
esac
case "$IN_PROFILE" in *" api "*)
  curl -sf -m 5 http://localhost:8002/docs >/dev/null 2>&1 \
    && say "api" "${GRN}응답${NC}" || { say "api" "${RED}응답 없음${NC}"; fail=1; } ;;
esac

echo "──────────────────────────────────────────────"
if [ "$fail" -eq 0 ]; then
  echo "${GRN}✅ 스택 정상 — 마운트·응답 모두 확인됨${NC}"
else
  echo "${RED}❌ 문제 있음 — 위 복구 명령 실행 후 재확인${NC}"
fi
exit "$fail"
