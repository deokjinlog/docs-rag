"""Inspector — 파싱 결과 육안 검수 화면 (읽기 전용).

`routes` 는 FastAPI·DB 를 끌어오므로 여기서 re-export 하지 않는다 — 단위 테스트가
`dup` 만 쓰려고 import 했다가 앱 전체를 로드하는 회귀를 막는다(CLAUDE.md 패키지 규칙).
"""
