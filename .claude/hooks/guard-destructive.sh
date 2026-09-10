#!/usr/bin/env bash
# 파괴적 Bash 명령을 사람 확인(ask)으로 돌린다.
#
# 왜 permissions.ask 규칙만으로 부족한가: 규칙은 명령의 **접두어**로 매칭된다. 그런데
# 2026-09-10 에 데이터를 날린 명령은
#     cat db/schema.sql | docker compose exec -T postgres psql ...
# 처럼 `cat` 으로 시작한다. `Bash(docker compose exec*)` 규칙은 여기 안 걸린다.
# 훅은 명령 문자열 전체를 보므로 파이프라인 어디에 있든 잡는다.
#
# 반환: 위험하면 permissionDecision=ask (사람 확인), 아니면 아무것도 안 하고 통과.
set -uo pipefail
cmd=$(jq -r '.tool_input.command // ""' 2>/dev/null) || exit 0
[ -z "$cmd" ] && exit 0

# 각 패턴은 실제로 겪은 사고이거나, 겪으면 복구가 안 되는 것들이다.
patterns=(
  'DROP[[:space:]]+TABLE'                       # 스키마 파괴 SQL 직접 실행
  'schema(_reset)?\.sql'                        # 그 파일을 어떤 형태로든 건드림
  'TRUNCATE[[:space:]]'
  # ⚠ 그냥 `docker compose down` 은 **볼륨을 안 지운다** — 데이터 파괴가 아니다.
  # 프로필 전환마다 물으면 경보 피로가 생겨 사람이 습관적으로 승인하게 된다.
  # 그건 가드가 없는 것보다 나쁘다. 볼륨을 함께 지우는 형태만 잡는다.
  'compose[[:space:]]+down[[:space:]].*(-v|--volumes)'
  'docker[[:space:]]+volume[[:space:]]+rm'
  'rm[[:space:]]+-rf?[[:space:]]+.*data'        # 코퍼스·산출물
  'rm[[:space:]]+-rf?[[:space:]]+.*model'
  '[-]X[[:space:]]*DELETE'                      # Qdrant 컬렉션 삭제 — 복구 불가
                                                # (앞이 '-' 면 grep 이 옵션으로 읽는다 → [-] 로 감쌈)
  'collections/[A-Za-z0-9_]+.*delete'
  'DELETE[[:space:]]+FROM'
)
for p in "${patterns[@]}"; do
  if printf '%s' "$cmd" | grep -qiE -- "$p"; then
    jq -nc --arg p "$p" '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "ask",
        permissionDecisionReason:
          ("파괴적 명령으로 보입니다 (패턴: " + $p + "). " +
           "2026-09-10 에 db/schema.sql 을 그대로 실행해 문서 26 · 청크 8,850 을 날린 적이 있습니다. " +
           "실행 전 `make backup` 을 떴는지 확인하세요.")
      }}'
    exit 0
  fi
done
exit 0
