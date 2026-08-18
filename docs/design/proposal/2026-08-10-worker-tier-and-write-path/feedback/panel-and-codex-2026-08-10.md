# 심사패널 + Codex 독립검토 대조 (2026-08-10)

## Codex 독립 결론 (리포를 직접 읽고 F1–F6를 자체 검증)

| 결정 | Codex 선택 |
|---|---|
| D1 | (b) 갱신 가능한 시간 리스 + 리더 선출 rescuer |
| D2 | (c) 하이브리드 — PG-first 아웃박스, 정상 경로 동기 드레인, 필요할 때만 지연 |

**Codex의 강한 입장**: "동기 git RPC를 기존 PG 트랜잭션 안에 넣는 Phase 3 gitd는 출하하지
않겠다. PG-first 커맨드 경로가 로컬에서 작동하기 전엔 프로덕션 트래픽을 그리로 보내지 마라."
→ **리포 채택 순서(gitd 먼저)를 뒤집으라**는 권고.

Codex 자기 반론: "D2는 현재 읽기·OCC·publication·자산·히스토리·응답 스키마를 전부 건드린다.
CAS + 드리프트 경보보다 훨씬 침습적이다. **그러나 동기 gitd를 먼저 출하하면 이미 복구
불가능한 로컬 dual-write를 복구 불가능한 네트워크 dual-write로 만든다.**"

Codex가 제 F2를 정정: vector-delete/S3-delete/events는 맞지만 인덱싱 청크는 별도 7일 reaper가
있음 → 과잉일반화. (제 자체 검증에서도 PARTIALLY로 나온 것과 일치.)

## 심사패널 종합

동일하게 D1 = (b) 만장일치, D2 = (c) 하이브리드. 그러나 **이음매를 통계 hoisting이 아니라
`GitService._stage_and_commit` + `proxy.mjs` 재시도 분류기에** 둠 — 지배적 잔재원이 rare
crash가 아니라 designed path(§4.1)이기 때문. 그리고 **PG-first는 유예** — `documents`에
정본 본문 없음 + 관측 인프라(PrometheusRule·ServiceMonitor·/metrics) 전무.

## 어디서 갈렸나 = PM 결정

| | Codex | 패널 |
|---|---|---|
| 순서 | PG-first 먼저, 동기 gitd RPC 뒤로 | gitd 먼저(진단·CAS·탐지기), PG-first는 그 뒤 |
| 이유 | 네트워크 dual-write 회피 | 관측 불가능한 기계장치로 역사 이전은 순서 오류 |

**일치(신뢰도 높음)**: PG-first가 최종 목적지 후보 / CAS 필요하되 불충분 / 결정론적 커밋
메타데이터 필요 / MCP 프록시 재시도 분류기 위험(양쪽 독립 발견) / `documents` 본문 부재가
PG-first의 진짜 비용.

## 심사역 불일치 (종합이 어느 쪽을 따랐나)

- **minimal의 F3 blast-radius 주장**: Judge B는 "D2를 결정하는 최고의 분석", Judge A는
  "materially incomplete". 종합은 **A를 따름** — minimal의 "reads are commit-pinned"은 GET엔
  참이나 read-modify-write(update/edit는 floating HEAD를 읽음)엔 거짓. 게다가 §4.1(write_lane
  designed path)이 "rare, never demonstrated" 프레이밍을 무너뜨림.
- **`terminal_policy` 파라미터**: Judge A 채택, Judge B 기각. 종합은 **B를 따름**(speculative
  generality, D2(b) 아티팩트).

## 열린 PM 결정 (README §8과 동일, 핵심만)

1. D2 순서 — Codex(PG-first 먼저) vs 패널(gitd 먼저).
2. M1 네이티브 revision 원장이 프로덕션용인가 — 이미 완전 구현이 트리에 있고 D2(b)의 절반일 수 있음.
3. 쓰기 HA가 실제 요구인가 — Option B도 이 계획도 안 줌.
4. 소스오브트루스 재정의("PG=현재, git=역사")를 수용하는가 — 서면 결정 사안.
