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

HAS_LLM=0
[ -s model/Qwen3-4B-AWQ/config.json ] && HAS_LLM=1

# 순서: 인프라 → vllm(느림, 있으면) → 앱.
#
# vllm 을 **먼저** 띄우는 게 맞다 — 서빙까지 2분 38초 걸리므로 일찍 시작할수록 빨리
# 준비된다. 앱은 수 초면 뜨고 vLLM 없이도 SQL·검색·거절 경로가 다 동작하니 기다릴 이유가
# 없다. 즉 "vllm 이 먼저 뜨는 것"은 문제가 아니었고, 문제는 compose 의 **hard 의존**이었다
# (vllm 생성 실패 → up 전체 중단 → api·celery 가 Created 로 방치).
# 그건 docker-compose.yml 의 `vllm: required: false` 로 근본 해결했다.
# 모델이 없으면 여기서 아예 건너뛴다 — 재시작 루프로 CPU 를 태우지 않기 위해.
echo "▶ 스택 기동"
docker compose up -d postgres qdrant rabbitmq odl paddle >/dev/null 2>&1
[ "$HAS_LLM" = 1 ] && docker compose up -d vllm >/dev/null 2>&1
docker compose up -d api celery flower >/dev/null 2>&1

echo "▶ 컨테이너 상태"
for svc in postgres qdrant rabbitmq odl paddle api celery; do
  st=$(docker compose ps "$svc" --format '{{.State}}' 2>/dev/null)
  if [ "$st" = "running" ]; then
    say "$svc" "${GRN}running${NC}"
  else
    say "$svc" "${RED}${st:-없음}${NC}"; fail=1
  fi
done

# vllm 은 모델 가중치가 있어야만 뜬다. 없으면 재시작 루프를 돌며 CPU 를 태우므로
# 실패로 세지 않되 **왜 안 뜨는지**는 정확히 알려준다.
if [ "$HAS_LLM" = 0 ]; then
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

check_mount() {  # $1=서비스 $2=컨테이너내경로
  local svc=$1 path=$2 n
  [ "$(docker compose ps "$svc" --format '{{.State}}' 2>/dev/null)" = "running" ] || return 0
  n=$(docker compose exec -T "$svc" sh -c "ls $path/output/raw/*.md 2>/dev/null | wc -l" 2>/dev/null | tr -d ' \r')
  if [ "${n:-0}" = "$host_n" ]; then
    say "$svc:$path" "${GRN}${n}개 ✓${NC}"
  else
    say "$svc:$path" "${RED}${n:-?}개 ✗ (호스트 ${host_n}개와 불일치)${NC}"
    echo "     └ 복구: docker compose up -d --force-recreate $svc"
    fail=1
  fi
}
check_mount odl    /data
check_mount paddle /data
check_mount api    /app/data
check_mount celery /app/data

# ── 의존 서비스가 실제로 응답하나 ────────────────────────────────────────────
echo "▶ 서비스 응답"
docker compose exec -T postgres pg_isready -U docsrag -d docsrag >/dev/null 2>&1 \
  && say "postgres" "${GRN}accepting${NC}" || { say "postgres" "${RED}응답 없음${NC}"; fail=1; }
curl -sf -m 5 http://localhost:6333/collections >/dev/null 2>&1 \
  && say "qdrant" "${GRN}응답${NC}" || { say "qdrant" "${RED}응답 없음${NC}"; fail=1; }
curl -sf -m 5 http://localhost:5002/health >/dev/null 2>&1 \
  && say "odl" "${GRN}응답${NC}" || { say "odl" "${YLW}health 무응답${NC}"; }
curl -sf -m 5 http://localhost:8002/docs >/dev/null 2>&1 \
  && say "api" "${GRN}응답${NC}" || { say "api" "${RED}응답 없음${NC}"; fail=1; }

echo "──────────────────────────────────────────────"
if [ "$fail" -eq 0 ]; then
  echo "${GRN}✅ 스택 정상 — 마운트·응답 모두 확인됨${NC}"
else
  echo "${RED}❌ 문제 있음 — 위 복구 명령 실행 후 재확인${NC}"
fi
exit "$fail"
