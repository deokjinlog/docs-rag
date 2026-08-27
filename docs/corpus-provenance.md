# 코퍼스 출처 명세 (corpus provenance)

이 프로젝트의 약관은 전부 **한국 보험사가 규제(보험업법)에 따라 공시 의무를 지는 상품약관**이다.
각 문서의 실제 회사·상품·공식 출처를 못박아 "회사미상"을 없앤다. 재수집은 아래 공식 출처에서만.

> **규제 근거**: 보험업법·감독규정상 보험사는 판매 상품의 약관을 **상품공시실**에 공개할 의무가 있고,
> 생·손보협회가 이를 **통합공시**로 집계한다. 따라서 약관의 1차 출처는 (a) 각사 상품공시실, (b) 협회
> 통합공시다. 임의 배포본이 아니라 이 경로에서 받은 것이 감사·인용 가능한 근거다.

## 공식 출처 (authoritative sources)

| 출처 | URL | 범위 |
|---|---|---|
| 손해보험협회 상품비교공시 (KPUB) | https://kpub.knia.or.kr/main.do | 손보 통합 (KB·AXA 등) |
| 생명보험협회 비교공시 | http://pub.insure.or.kr | 생명 (라이나 등) |
| 보험다모아 (금융위·협회 공동) | https://www.e-insmarket.or.kr | 상품 비교·공시 포털 |
| KB손해보험 상품목록(약관) | https://www.kbinsure.co.kr/CG802030001.ec | KB 약관 직접 다운로드 |
| 라이나생명 상품공시실 | https://www.lina.co.kr/disclosure/product-public-announcement/product-guide | 라이나 약관 |
| AXA손해보험 | https://www.axa.co.kr | AXA(다이렉트 포함) 약관 |

## 문서별 출처 (12개 · 3개 회사로 정리 · 회사미상 0)

| 파일(현재) | 실제 회사 | 상품 | 공식 출처 |
|---|---|---|---|
| KB_골든라이프케어간편건강보험(26.01) | KB손해보험 | 간편심사 건강(치매·간병) | kbinsure.co.kr 상품목록(약관) |
| KB_슬기로운간편실속종합건강보험(23.11) | KB손해보험 | 종합건강 | kbinsure.co.kr |
| KB_플러스운전자상해보험(26.01) | KB손해보험 | 운전자상해 | kbinsure.co.kr |
| KB_희망플러스자녀보험II(21.07) | KB손해보험 | 자녀보험 | kbinsure.co.kr |
| 보험약관_상해질병보장_**회사미상** | **LIG손해보험 → 현 KB손해보험** | 장기 상해·질병 | kbinsure.co.kr (구 LIG 상품) |
| 보험약관_수술비보장_**회사미상** | **LIG손해보험 → 현 KB손해보험** | 장기 수술비 | kbinsure.co.kr (구 LIG 상품) |
| 라이나_중환자실입원특약 | 라이나생명 | 중환자실 입원 특약 | lina.co.kr 상품공시실 |
| 라이나_소득보장수술특약 | 라이나생명 | 수술 소득보장 특약 | lina.co.kr |
| 라이나_간병인사용입원특약 | 라이나생명 | 간병인 입원 특약 | lina.co.kr |
| 라이나_든든한실버치아보험 | 라이나생명 | 실버 치아 | lina.co.kr |
| New치아보험 | AXA손해보험 | 치아 | axa.co.kr / kpub.knia |
| 다이렉트늘안심입원비보험 | **AXA손해보험** (다이렉트) | 입원비 | axa.co.kr |

### "회사미상" 해소 근거 (문서 내부 식별)
- **상해질병·수술비**: 본문에 `LIG손해보험`·`www.lig.co.kr`·`1544-0114` 다수 → **LIG손해보험**. LIG는
  2015년 KB금융이 인수해 **현 KB손해보험**(구 LIG 상품은 KB 승계). → 실질 KB.
- **다이렉트 늘안심입원비**: 본문에 `www.axa.co.kr`·`1566-1566` → **AXA손해보험** 다이렉트 채널.

→ **정리 결과: 회사미상 0. 전부 KB손해보험 · 라이나생명 · AXA손해보험 3개사.**

## 재수집 계획 (clean re-collection)

깔끔한 재처리를 원하면 아래 순서:
1. **출처 고정**: 각 문서를 위 공식 URL에서만 받는다(협회 통합공시 KPUB/pub.insure 우선 — 회사·상품코드·
   공시일 메타데이터가 붙어 감사 가능).
2. **파일명 정규화**: `회사미상` → `KB_구LIG_상해질병` 등 실제 회사 반영(단, 현 DB `product_id`·경로가
   바뀌므로 재적재 필요 — 무단 rename 금지, 재수집 시 일괄).
3. **provenance 메타 적재**: 각 문서에 `source_url`·`상품코드`·`공시일`을 `product.source_doc` 확장 컬럼으로
   기록 → "이 값이 어느 공시본에서 왔나"를 DB에서 추적.
4. **재파싱**: 새 PDF로 reconstruct→parse→stage→gate→extract 재실행, 골든은 그대로(원문 불변이면 무회귀).

> 주의: 공시 포털은 상품 검색 UI(JS)라 단순 wget이 안 될 수 있음 → 상품코드로 직접 PDF URL을 얻거나
> 브라우저 자동화 필요. 재수집은 별도 데이터 작업으로 스코프.

## 참고 (검증 링크)
- 손해보험협회 통합공시: https://kpub.knia.or.kr/main.do · 소비자포털 https://consumer.knia.or.kr/disclosure.do
- 생명보험협회 비교공시: http://pub.insure.or.kr
- 보험다모아: https://www.e-insmarket.or.kr
- KB손해보험 약관: https://www.kbinsure.co.kr/CG802030001.ec
- 라이나생명 상품공시실: https://www.lina.co.kr/disclosure/product-public-announcement/product-guide
- (LIG→KB 인수: 2015년 KB금융의 LIG손해보험 인수 → KB손해보험으로 사명 변경)
