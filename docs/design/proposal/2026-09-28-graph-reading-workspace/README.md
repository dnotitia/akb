---
status: proposal
stage: implemented-pending-review
created: 2026-09-28
updated: 2026-09-29
---

# Graph workspace — Obsidian 스타일의 관계 탐색기

## 요구사항 정정

행·열 정렬은 폐기하고 회전 가능한 그래프를 유지하되, 사용자가 원하는 외형은
**Obsidian처럼 작은 점·가는 선·차분한 이름표**다. 구·큐브 등 과장된 입체 재질은
요구를 잘못 해석한 결과이므로 제거한다. 실제 x/y/z 좌표와 가로·세로 회전은
유지하되, 깊이는 탐색 동작에만 쓰고 도형의 광택·조명으로 강조하지 않는다.
기존 공통 셸, 검색·필터·목록·Inspector, 권한과 API 호환성은 유지한다.

## 레퍼런스와 선택

- [Obsidian Graph view](https://help.obsidian.md/plugins/graph): 작은 원형 점,
  연결 수에 따른 절제된 크기 차이, 가는 선과 겹침을 줄인 제목.
- [3D Force Graph](https://vasturiano.github.io/3d-force-graph/): 입체 force 배치와
  리소스 선택 중심의 탐색. 자동 회전, 파티클, 데이터 없는 가상 관계는 가져오지 않는다.
- [Three.js](https://threejs.org/docs/)와
  [OrbitControls](https://threejs.org/docs/pages/OrbitControls.html): WebGL 원근 카메라,
  camera-facing unlit dots, 드래그 궤도 회전·우클릭 이동·휠 확대.
- [d3-force-3d](https://github.com/vasturiano/d3-force-3d): 세 좌표가 참여하는
  force 계산을 Worker에서 제한된 tick 수로 실행한다.
- [Neo4j Bloom](https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/bloom-overview/):
  그래프가 주 영역, 검색과 선택 정보는 보조 영역이라는 역할 분리를 참고한다.
- [Primer SegmentedControl](https://primer.style/product/components/segmented-control/)와
  [ActionBar](https://primer.style/product/components/action-bar/): Graph/List는
  배타적 보기 선택으로 묶고, 필터·추가 도구는 구분선으로 분리한다.
- `frontend-design`: 그래프가 주인공이 되도록 불필요한 카드·대시보드 장식을 제거한다.
- `ui-ux-pro-max`: 탐색용 OrbitControls, 드래그의 버튼 대안, 키보드 경로,
  색 이외의 종류 구분을 적용한다. AKB 토큰·Pretendard·Lucide는 유지한다.

**기술 선택:** Three.js + OrbitControls + d3-force-3d Worker.
직접 렌더러를 구성해 카메라·키보드·투영 라벨·리소스 수명을 제어한다.
Cytoscape/fCoSE와 react-force-graph-2d는 유지하지 않는다. 하나의 3D 렌더러만 사용한다.

## 화면과 시각 구조

```text
공통 앱 셸 / Vault 내비게이션 — 기존 유지
작은 리소스 검색                  Graph · List | Filters · 더보기
Show  ● Documents  ■ Tables  ◆ Files   [집중 탐색일 때만 경로·hop]
┌────────────────────────────────────────────────────┐
│ 조작 안내                                           │
│                                                    │
│            작은 평면 표시와 실제 관계선    선택 Inspector│
│                                                    │
│ 표시 리소스·관계·범위             회전 / 이동·확대·Fit │
└────────────────────────────────────────────────────┘
```

- 문서 원형(`cat-1`), 테이블 사각형(`cat-3`), 파일 마름모(`cat-4`)로 구분한다.
  범례와 같은 작은 평면 표시만 사용하며 조명·광택·입체 도형은 없다.
  점은 화면 기준 반지름 4.5–6.5px, 선택 시 +1.5px로 제한하여 단일 항목이나
  확대 상태에서도 커다란 도형이 되지 않는다. 크기는 현재 지도 내 연결 수에 따른다.
  종류는 모양·범례·tooltip·List·필터·Inspector에서 명시하며 색에만 의존하지 않는다.
- 선택 리소스는 종류 색상을 유지하고 바깥 링으로 표시한다. 직접 연결을 강조하고
  나머지는 흐리게 하되 삭제하지 않는다.
- 도구 모음은 검색 / 보기 / 추가 도구의 역할을 분리한다. 아래의 종류 범례는
  다중 선택 필터로 동작하며 개수는 현재 로드한 지도 기준이다. 숨긴 종류도 개수와
  버튼을 유지해 다시 표시할 수 있고, 관계 필터·선택·탐색 중심은 그대로 보존한다.
  좁은 화면에서는 검색을 별도 행으로 두고 터치 영역을 44px로 확보한다.
- 제목은 배경·테두리·상시 아이콘을 제거한 DOM 텍스트다. 겹치는 라벨은 생략하지만 노드는 그린다.
  선택·hover·연결·pin 순으로 우선하고, 전체 제목은 검색·목록·Inspector에서 확인한다.
- 실선은 명시적 관계, 점선은 본문 링크다. 출처가 없으면 추측하지 않는다.
  중복·역방향 관계는 곡선으로 분리하고 자기 관계는 loop로 그린다.
- 선을 누르면 방향, 관계명, 출처를 보여준다. 장면 위의 입체 화살표는 제거한다.
- light/dark 모두 중앙 토큰을 사용한다. 그래프 배경을 임의의 별도 테마로 고정하지 않는다.

## 상호작용

| 입력 | 동작 |
| --- | --- |
| 왼쪽 드래그 | 실제 3D 카메라 궤도 회전 |
| 오른쪽 드래그 / 터치 두 손가락 | 화면 이동 |
| 휠 / pinch | 확대·축소 |
| 회전·이동·확대·Fit 버튼 | 드래그 없는 동일 조작 |
| Canvas에 포커스 후 방향키 | 회전; Shift + 방향키는 이동 |
| + / − / Home | 확대·축소·전체 보기 |
| 노드 또는 라벨 클릭 | 관계 강조와 Inspector; 원문으로 자동 이동하지 않음 |
| Inspector Preview / Open in vault | 모달 미리보기 / 리소스 본문 이동 |
| Explore connections / Load connections | 새 중심으로 탐색 / 현 장면에 이웃 추가 |

카메라는 자동으로 돌지 않는다. 선택이 보조 패널 뒤나 카메라 뒤에 있을 때만
읽을 수 있는 영역으로 이동한다. 정상적으로 보이는 선택의 카메라는 유지한다.
Inspector는 Canvas 폭을 줄이지 않는 overlay이며 작은 화면은 하단 sheet다.
Graph/List는 같은 장면·필터·개수를 공유한다. 모바일 기본은 List, 3D 전환은 유지한다.
WebGL2가 없거나 context가 끊기면 명시적 안내와 List 진입점을 제공한다.

## 배치와 상태

- 최초 및 명시적 Rearrange: 3D link/charge/collision force, Worker 200 ticks.
  비연결 노드도 입체 공간에 유지하며 행렬로 정렬하거나 가상 선을 추가하지 않는다.
- 선택·테마·필터는 배치를 재실행하지 않는다. 숨긴 노드의 좌표도 보존한다.
- 추가 로드: 기존 연결점 근처에 새 노드를 배치하고 기존 좌표를 고정한 incremental
  force를 계산한다. 기존 카메라를 자동 초기화하지 않으며 Fit은 명시적 동작이다.
- 요청 세대/Worker 종료로 이전 결과가 새 장면을 덮지 못하게 한다.
- GPU 객체, controls, 이벤트, Worker, 타이머는 unmount 시 정리한다.
- 0개 리소스, 관계만 0개, 필터 결과 0개, 로딩, 오류를 구별한다.

## 데이터·백엔드 경계

API는 변경하지 않는다. 최초 overview는 연결 리소스 최대 200개와 미연결 최대
500개를 제공할 수 있다. 연결된 전체 수와 미연결 수를 섞어 잘못된 합계를 만들지 않는다.
선택적 메타데이터가 없으면 완전성을 unknown으로 둔다. BFS 경계의 모든 선이
반환된다고 약속하지 않으며 서버에 없는 정확한 잔여 수·유사도·최단경로를 만들지 않는다.

문서·테이블·파일 추가 로드는 canonical URI로 요청한다. 필터·숨김은 Canvas,
List, Inspector, 개수 모두 같은 모델에 적용한다. 사용자/권한 변경은 장면을
초기화하는 보안 경계이며, 일반 미리보기는 카메라를 유지한다.

## 검증

구현 결정과 회귀 검증 범위는 [구현 기록](implementation.md)에 적는다.

- 순수 배치: 네 점의 스칼라 삼중곱으로 실제 부피 검증. 단순 z값 존재만 검사하지 않는다.
- WebGL2 실제 브라우저: 빈/단일/미연결/혼합 리소스, 양축 회전의 픽셀·라벨 변화,
  선 클릭, 미리보기 왕복, light/dark, 모바일, 버튼 조작, 필터와 추가 로드 실패/재시도.
- 700/1,500개 fixture: 계산과 렌더링 검증. 결과를 운영 성능 보장으로 표현하지 않는다.
- 디자인 토큰·타입·린트·전체 단위 테스트와 프로덕션 빌드.
- 로컬 프런트만 교체. 백엔드·사용자 데이터·프로덕션 변경 없음.

완료 판정은 “3D처럼 보이는가”가 아니라 **실제 공간 회전이 되고, 리소스와 관계를
선택하고 읽으며, 원문에서 돌아와 탐색을 이어갈 수 있는가**다.
