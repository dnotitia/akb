---
status: accepted
stage: applied
created: 2026-09-16
updated: 2026-09-16
---

# Unified exact grep

## 목적과 제공 범위

`postgres_native`에서 `akb_grep`의 원문 조회, 정확한 일치 행 집계,
리소스 목록과 Document 일괄 치환을 같은 범위·매칭 규칙으로 제공한다.
REST/MCP와 Search UI가 그 계약을 공유한다. 기존 Native body reader와
mutation service를 재사용하며 별도 검색 엔진이나 새 저장 계층은 추가하지 않는다.

필수 로드맵은 G0–G5다. legacy/Bare Git 개선은 제외한다. 공용 모델의 최소
호환만 유지한다. File 치환(F1), 과거 revision 검색, ripgrep 가속기는 선택
기능이며 이번 완료 조건에 포함하지 않는다. binary/OCR 검색과 새 권한 모델도
범위 밖이다. 이 문서는 구현·검증 결과를 반영한 설계 기록이며 배포 기록은 아니다.

## 확정한 계약

### 입력과 범위

- Document-only가 기본이다. `include_text_files=true`로 native File 조회를
  명시적으로 선택한다. `measurement_include_text_files`는 호환 alias다.
  두 값이 명시되고 다르면 저장소 접근 전에 검증 오류를 반환한다.
- REST/MCP는 Vault 목록, collection, doc_types, tags, include_archived와
  archive_scope를 지원한다. MCP의 기존 단일 Vault 문자열도 유지한다.
  Vault/type/tag 내부는 OR, 서로 다른 필터와 ACL은 AND다.
- collection은 해당 경로와 하위 경로만 포함한다. 비슷한 이름의 sibling은
  제외하고 `%`, `_`, 역슬래시는 SQL wildcard로 해석하지 않는다.
- doc_types/tags와 archived-only는 File을 제외한다. unarchived 조회는 File을
  허용한다. grep의 기존 archive 기본값은 유지하며 UI는 선택값을 명시한다.
- REST/MCP 출력 limit은 1–50이다. limit은 출력만 자르며 exact count와
  Document 치환 대상을 자르지 않는다. 치환에는 독립 write budget이 있다.
- File 옵션을 켠 치환은 사전에 거절한다. MCP 치환은 명시적인 Vault 범위와
  각 Vault 쓰기 권한이 필요하다.

### 원문·매칭·결과

Native Head의 본문을 읽는다. 파생 chunk/index는 grep의 원문 authority가
아니다. ACL과 필터를 먼저 적용하고 같은 repeatable-read snapshot에서 File
최신성, 대상 개수·용량과 payload를 확인한다. payload 누락도 성공한 빈 결과로
취급하지 않는다.

Literal은 substring이다. 대소문자 무시 literal은 escaped Python regex와
동일한 Unicode 의미를 사용하므로 `ß`/`ss` 전체 casefold 확장은 일치하지 않는다.
Regex와 치환은 같은 본문 행 단위로 동작한다. 따라서 행을 넘는 공백이나 anchor가
조회에 없던 영역을 치환하지 않는다. literal replacement의 역슬래시는 그대로
쓴다. 기존 Document 입력 정규화는 유지하므로 raw File bytes 보존 약속은 아니다.

정규식과 대소문자 무시 literal은 기존 별도 process worker에서 실행하며 5초
deadline과 결과 크기 제한을 적용한다. 취소·timeout·범위 초과는 성공한 일부
count나 zero-match로 위장하지 않는다. 정확한 count는 완료된 전체 scope의
**일치 행 수**이며 한 행의 여러 occurrence 수가 아니다.

결과에는 URI, resource_type, 검색한 revision, content_hash, body-relative
line이 남는다. 명시적 `include_text_files=true` 응답은 다음처럼 분리한다.

| 모드 | 전체 리소스 | Document 부분집합 |
|---|---|---|
| 조회 | total_resources / returned_resources | total_docs / returned_docs |
| count | by_resource | by_doc |
| list | resources / n_resources | files / n_files |

`resources` 항목은 uri/resource_type/revision/path를 가진다. 호환 alias만 사용한
기존 호출은 이전 응답 의미를 유지한다. UI는 새 리소스 집계를 사용하고 File
링크를 Document 미리보기로 전달하지 않는다. File 선택과 필터는 URL에 보존한다.
409/503 등 불완전 상태는 검색 불가 안내와 재시도로 표시한다.

### text File 분류와 최신성

`text_file`은 개념적 표현이다. 실제 Native 분류는 `surface=file`,
`content_profile=text`다. 일반 File은 `text/*` MIME과 크기 조건을 통과하고,
본문 digest/size, strict UTF-8, NUL 부재까지 검증되어야 한다. 확장자만으로
`.json`, `.py` 등의 지원을 약속하지 않는다.

일반 File의 원본 authority는 S3와 `vault_files`다. Native 검색 projection은
비동기다. catalogue만 있고 Head가 없는 새 File, 수정·삭제 대기, abandoned
projection, 경로/identity/본문 불일치는 `grep_file_projection_not_ready` 409다.
권한 밖 File은 readiness 검사에도 노출되지 않는다. File을 제외하는 필터에는
관계없는 File 지연이 영향을 주지 않는다. 현재 원본과 일치하는 완료된 outbox가
UTF-8/NUL 실패를 확인한 경우는 정상적인 binary 제외다.

기존 이관에서는 verified cutover receipt가 충돌 회피 applied_path와 binary
제외의 근거가 된다. 새 outbox가 없고 receipt가 현재 catalogue와 정확히 일치할
때만 이 근거를 인정한다. 단순히 hash가 같다는 이유로 경로 조건을 제거하지 않는다.
Guarded native_text File은 별도 native resource/revision 연결도 검증한다.
일반 outbox는 path/MIME/key/hash/size의 검증된 내용 identity를 사용한다.
ETag/storage version이 outbox에 없으므로 object generation 보증은 하지 않는다.
이관 receipt는 해당 필드도 포함하므로 현재 catalogue와 함께 검증한다.

File 치환을 추가한다면 이 텍스트 분류는 필요조건일 뿐이다. public File mutation의
쓰기 authority, source-managed/read-only 제한, 원본 CAS, catalogue/projection
일치와 재시도 복구가 먼저 필요하다. grep projection만 직접 변경하지 않는다.

### Document 치환과 실패

치환은 전체 대상의 권한과 budget을 확인한 다음 각 Document의 쓰기 권한을 다시
확인하고 revision CAS로 기록한다. 동시 수정은 덮어쓰지 않는다. 여러 Document를
묶은 원자적 transaction은 아니다. 부분 실패는 완료 receipt, 이전 revision,
실패 URI와 완료 개수를 반환한다. 요청을 통째로 무조건 재시도하면 중복 치환될 수
있으므로 receipt와 현재 revision을 확인한 뒤 남은 대상만 재시도한다.

## 작업 결과와 피드백

| 단계 | 구현 결과 | 검증 |
|---|---|---|
| G0 | native 회귀 baseline, 기존 이슈 영향 분리 | 실제 PG collection sibling 무변경, budget 초과 쓰기 0, limit=1 전체 치환, CAS 경쟁·부분 receipt |
| G1 | REST/MCP 필터, alias 충돌, limit 정합 | schema/service/adapter 계약 테스트 |
| G2 | 원문 literal/regex/count/list/Document 치환 의미 일치 | Unicode·행 경계·literal 역슬래시·bounded worker 회귀 |
| G3 | File opt-in, identity·revision, catalogue와 projection 검증 | 실제 PG 생성·수정·실패·삭제·binary 제외·ACL·REST/MCP 응답 |
| G4 | SDK와 Search UI 필터·리소스 집계·위치·오류 | SDK 60, frontend 1,250, Chromium 4 시나리오 |
| G5 | 작은 파일/긴 문서/count/악성 regex 측정, 호환 안내 | 아래 측정과 최종 회귀 결과; 로컬 검증이며 미배포 |

구현 중 피드백으로 세 가지를 수정했다. 첫째, case-insensitive literal도 반복
접두사 입력에서 비싼 연산이 될 수 있어 bounded worker로 옮겼다. 둘째, live Head만
읽으면 생성 직후 File을 누락하거나 과거 projection을 최신 결과로 오인하므로
catalogue/outbox와 양방향으로 대조한다. 셋째, 이관된 File의 applied_path와
verified binary 제외를 명시적으로 인정해야 정상 이관 자료가 영구 409가 되지 않는다.

## 측정과 한계

격리된 로컬 프로세스의 3회 측정 중앙값이다. DB 읽기·네트워크·인증은 제외했으므로
end-to-end SLO가 아니다. RSS는 import를 포함한 프로세스별 최대값이다.

| workload | latency | 결과 |
|---|---:|---|
| 10,000 documents / 2.54 MB, case-sensitive | 81.57 ms | 정확한 전체 count, 20 snippet만 출력 |
| 동일 corpus, case-insensitive worker | 523.25 ms | spawn 비용 포함 |
| 1 document / 7 MB, case-sensitive | 64.21 ms | 긴 본문 처리 |
| 동일 본문, case-insensitive worker | 559.11 ms | spawn 비용 포함 |
| 10 documents / 1,000,000 matching lines / 7 MB, count | 94.16 ms | 정확히 1,000,000; snippet 0 |
| `(a+)+$`, 30,000 a와 실패 suffix | 5,007.24 ms | deadline으로 종료 |

작은 문서 사례의 scanner 결과 JSON은 7,608 JSON bytes였다. parent/child의 import-inclusive
peak RSS는 각각 약 74–127 MiB이며 두 peak를 동시 사용량처럼 합산하지 않는다.
약 0.45초 worker 시작 비용이 관측됐지만 이를 이유로 안전 경계를 없애거나 새
persistent worker/ripgrep 계층을 도입하지 않는다. 이 수치는 최종 matcher에서 재측정했으며 public API 응답 크기나 서버 처리량의
보증은 아니다. 측정 피드백으로 상세 Document 집계 map을 count 모드에만
생성하도록 바꿔 조회 worker 결과가 306,496 bytes로 커지는 낭비를 제거했다.

## 검증 범위와 릴리스 조건

실제 PostgreSQL 검증은 production SQL/native service/projection worker를 사용했다.
REST는 ASGI로 production route/serializer/error handler를 호출하고 인증 identity를
fixture로 주입했다. MCP는 실제 handler/service를 호출했다. S3 bytes는 fixture다.
이 검증은 실제 로그인·MCP transport·외부 S3 연동을 증명하지 않는다.

브라우저 검증은 실제 Chromium의 desktop/mobile, light/dark에서 API fixture를
사용했다. URL 복원, File 선택, 문서/File 링크, 오류 상태와 요청 직렬화를 확인했다.
실제 DB 검증과 브라우저 검증을 하나의 live end-to-end 테스트로 표현하지 않는다.

Backend/frontend/SDK를 함께 릴리스해야 한다. 이전 backend는 새 옵션을 무시할 수
있으므로 frontend-only 배포는 지원하지 않는다. 별도 schema migration은 추가하지
않으며 기존 native projection/cutover migration을 적용한 환경이 전제다.
실제 배포 이후 환경별 smoke 확인은 이 로컬 작업의 검증과 구분한다.

필수 **G0–G5의 구현과 로컬 검증을 완료**했다. 최종 리뷰에서 추가 blocker는
발견하지 못했다. 이관 예외와 권한 회수·누락 payload까지 포함한 기록은 다음과 같다.

| 검증 | 결과 |
|---|---|
| grep/replace/filter/MCP 계약 단위 테스트 | 100 passed (추가 payload 회귀 포함) |
| 실제 PG File·치환 measurement suite | 39 passed (권한 회수 회귀 포함) |
| 실제 PG projection·reference ambiguity·cutover suites | 33 passed |
| SDK | 60 passed |
| Frontend | 1,250 passed; design/type/lint/build 통과 |
| Chromium contract | 4 passed; desktop/mobile, light/dark |
| Backend static | 전체 Ruff, mypy 377 files, Bandit medium gate 통과; skipped 0 |
| Repository/doc | agent-role generated 8 files 일치, diff whitespace·문서 링크·공개 안전성 확인 |

Frontend lint의 기존 warning 105개와 Bandit low 항목은 이번 변경에서 새로
해결할 범위가 아니다. Bandit medium/high 항목은 없다. Measurement DB는
`akb_revision_m1_measurement_*` 이름으로 실행해야 migration 057의 placement
계약이 적용된다. 기본 postgres DB에서 이를 실행한 초기 실패는 올바른 격리
measurement DB로 재실행해 해소했다. 검사 실패를 결과에서 생략하지 않고
fixture 환경과 구현 결함을 구분했다.

## 이슈 취합: 상태와 코드 사실을 분리

| 이슈 | GitHub 상태 | 현재 코드와 계획에서의 처리 |
|---|---|---|
| [#41](https://github.com/dnotitia/akb/issues/41) count/list 모드 | CLOSED | `count_only`, `files_with_matches` 제공 중. 신규 도구를 만들지 않고 기존 모드의 정확성과 응답 호환을 보존한다. |
| [#315](https://github.com/dnotitia/akb/issues/315) 출력 limit이 치환 범위를 자름 | CLOSED | [#348](https://github.com/dnotitia/akb/pull/348)에 전체 집합 치환, 독립 budget, CAS와 복구 receipt 반영. 회귀 기준이다. |
| [#338](https://github.com/dnotitia/akb/issues/338) collection 경계 초과 치환 | OPEN | native의 anchored/escaped 조건과 실제 PG 대상·sibling 무변경을 확인했다. legacy 종료 판단은 별도다. |
| [#339](https://github.com/dnotitia/akb/issues/339) 사용자 TITLE/URI 행 제거 | OPEN | 현재 stripper는 builder 형태와 필수 PATH를 확인한다. native 원문 조회에서 해당 문단이 보존되는지 확인한다. legacy chunk stripper와 search/drill_down 개선은 이번 범위에서 제외한다. |
| [#341](https://github.com/dnotitia/akb/issues/341) literal 치환의 역슬래시 해석 | OPEN | 공유 `apply_grep_replacement`가 callable replacement를 사용한다. native의 대소문자 옵션 양쪽을 회귀 검증한다. legacy를 포함한 이슈 전체 종료 판단은 별도로 남긴다. |
| [#342](https://github.com/dnotitia/akb/issues/342) native 치환 출력 제한 | OPEN | 현재 native도 전체 일치 집합과 독립 `max_replacements`를 사용한다. 옛 구현을 전제로 재구현하지 않는다. |

관련 이슈는 동일 기능의 신규 요구와 구분한다.

- [#340](https://github.com/dnotitia/akb/issues/340): UUID/path 충돌은 현재
  ambiguity 오류로 처리한다. grep에서 읽은 리소스와 치환 대상이 같다는
  통합 회귀 사례로 포함한다.
- [#35](https://github.com/dnotitia/akb/issues/35): CLOSED. 반환 개수와 전체
  개수를 구분하는 선행 계약이다. semantic search의 candidate count를
  exact grep의 전체 일치 수로 재사용하지 않는다.
- [#407](https://github.com/dnotitia/akb/issues/407): OPEN. 기존 effective Vault
  access를 그대로 소비하고, 이 작업에서 더 세밀한 권한 모델을 새로 만들지 않는다.
- [#542](https://github.com/dnotitia/akb/issues/542): OPEN. agent memory의 legacy
  catalog 의존은 인접 문제다. grep의 authoritative-body 검증과 같은 원칙을
  적용할 수 있지만 이 기능의 release 선행조건으로 묶지 않는다.

OPEN은 미구현과 동의어가 아니다. 위 수정은 코드 확인이며, 모든 배포에 적용됐다는
의미도 아니다. 이 계획 작성에서는 이슈를 수정하거나 닫지 않았다.

