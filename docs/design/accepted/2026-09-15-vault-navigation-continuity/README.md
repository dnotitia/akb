---
status: accepted
stage: implemented
created: 2026-09-15
updated: 2026-09-15
---

# Vault navigation — 읽기 중에도 이어지는 작업공간

## 최종 결정

**Collections는 콘텐츠 탐색 전용으로 유지한다. Vault 작업 페이지와 전체
문서·파일·테이블 뷰어에 동일한 내비게이션 한 줄을 유지한다.**
최상단 브레드크럼브와 기존 Workspace/Vault 목록은 유지한다.

검토 과정에서 다음 안을 철회했다.

1. 최상단 수평 Vault 메뉴 + 아래로 내려간 브레드크럼브:
   현재 위치와 목적지 목록의 우선순위가 뒤집혀 보였다.
2. Collections 위아래에 Vault 탐색/관리 메뉴 배치:
   이동은 일관됐지만 컬렉션 사이드바가 콘텐츠 탐색이라는 본래 역할을 잃었다.
3. 작업 메뉴에서 Overview 생략:
   Vault 이름으로 복귀할 수는 있지만, Overview 화면에서 선택된 메뉴가 없고
   처음 사용하는 사람이 해당 진입점을 발견하기 어려웠다. 첫 항목으로 복원한다.
4. 전체 리소스 뷰어에서 작업 메뉴 생략:
   읽기 높이는 확보했지만 다른 Vault 기능으로 가려면 먼저 Overview로 돌아가야
   했다. 일반 조회가 별도의 집중 모드처럼 작동하고 익숙한 메뉴가 사라지는 문제가
   확인되어 철회한다. breadcrumb는 부모로 복귀하는 수단이지 형제 기능의 대체물이 아니다.

최종안은 **위치 / Vault 이동 / 리소스 조작**을 분리한다. 데스크톱에서 약 40px의
추가 높이를 사용하지만 문서를 읽다가 Search·Graph·Public links·관리 페이지로 바로
이동할 수 있고, 작업 페이지와 뷰어 사이의 메뉴 위치가 고정된다. 큰 제목·설명·카드를
추가하는 것이 아니다. 미리보기 모달은 별도의 집중된 읽기 표면으로 유지한다.
[읽기 작업공간 설계](../2026-09-14-resource-reading-workspace/README.md)의
본문·편집·권한·미리보기 계약은 유지한다.

## 정보 구조

| 영역 | 역할 |
| --- | --- |
| Workspace 사이드바 | Home, Search, Vaults, 계정 Settings |
| Vaults 사이드바 | Vault 목록·전환; 선택된 Vault 재클릭도 Overview로 이동 |
| Collections 사이드바 | Collection 이름, 관리·필터, 리소스 트리만 표시 |
| 최상단 앱 헤더 | 현재 위치 브레드크럼브 + 전역 검색·인덱싱·알림·프로필 |
| Vault 공통 이동 행 | 왼쪽 Overview / Search / Graph / Public links; 오른쪽 끝 Members / Settings |
| 리소스 도구막대 | 현재 문서·파일·테이블의 읽기·편집·발행 등 조작 |

## 두 화면의 골격

작업 페이지 — Overview, Search, Graph, Public links, Members, Settings, Activity:

```text
Vaults | All collections       | Vault / 현재 페이지                 검색 · 계정
───────┼───────────────────────┼───────────────────────────────────────────────
관리   | Collections 관리      | Overview Search Graph Public links    Members Settings
필터   | 필터                  | ─────────────────────────────────────────────
목록   | 컬렉션·리소스 트리    | 해당 페이지 본문
```

리소스 읽기 — Document, File, Table:

```text
Vaults | All collections       | Vault / Collection / 제목           검색 · 계정
───────┼───────────────────────┼───────────────────────────────────────────────
관리   | Collections 관리      | Overview Search Graph Public links    Members Settings
필터   | 필터                  | 통계/요약        [Rendered | Raw] Copy Edit Publish ⋯
목록   | 컬렉션·리소스 트리    | ─────────────────────────────────────────────
       |                       | 본문
```

- 작업 페이지의 내비게이션은 본문 스크롤 영역 밖에 있으며 새 제목 카드가 아니다.
- 전체 리소스 뷰어에도 같은 행을 표시한다. 문서 도구막대는 그 아래에 별도로 유지한다.
- 생성 페이지와 guide redirect도 별도 작업 페이지 메뉴를 표시하지 않는다.
- Overview를 첫 메뉴로 둔다. 기존 Vault 목록 링크와 브레드크럼브의 Vault
  링크도 동일한 Overview 목적지로 연결된다. 복귀 가능성과 현재 위치 표시는
  별개의 역할이므로 이 중복은 의도적이다.
- Vault를 선택하지 않았다면 전용 메뉴나 Collections 열을 만들지 않는다.
- 문서 Publish는 현재 문서의 발행 동작, Public links는 Vault 전체 공개 링크
  목록이다. 상단 페이지 위치에도 Public links라는 같은 명칭을 쓴다.

## Collections의 복원

- `In this vault`, 상단 세 개 탐색 링크, 하단 세 개 관리 링크를 제거한다.
- 원래 `VaultExplorer`가 identity 행을 소유한다. All collections 또는 현재
  Collection 이름, Folder 아이콘, Collapse collections 버튼을 보여준다.
- identity 56px / management 40px / filter 40px의 데스크톱 정렬을 Vaults와 맞춘다.
- 생성·업로드·테이블 생성·필터·컬렉션 세부 정보·리소스 메뉴는 기존 구현을 사용한다.
- 리소스 트리가 남는 높이를 사용하며 독립적으로 스크롤한다.
- 명시적으로 접으면 40px 스트립의 Expand collections 버튼으로 복원한다.
- 페이지 종류가 바뀐다는 이유로 자동 접기/펼치기를 하지 않는다. 저장된 열 폭과
  접힘 선호를 유지하며 Collection 딥링크의 일시적 펼침도 저장 선호를 덮어쓰지 않는다.

## 스타일과 반응형

- 기존 AKB surface, foreground-muted, link, border, focus-ring 토큰을 사용한다.
  Pretendard 14px과 Lucide 16px 아이콘을 유지하고 새 글꼴·팔레트를 도입하지 않는다.
- 작업 메뉴는 카드·개별 테두리 버튼·채워진 선택 탭이 아닌 밑줄형 링크다.
  현재 위치는 teal 텍스트·600 굵기·2px 밑줄로 표시하며 hover는 중립색이다.
- Overview / Search / Graph / Public links는 왼쪽의 콘텐츠 탐색 그룹,
  Members / Settings는 오른쪽 끝의 관리 그룹으로 둔다. 사용 빈도를 측정한
  결과가 아니라 작업의 목적에 따른 분리이며 권한이 없다는 뜻도 아니다.
  두 그룹의 링크·아이콘·활성 밑줄은 동일하게 유지한다. 추가 관리 제목,
  카드, 배경색으로 또 다른 계층을 만들지 않는다. 좁은 폭의 More는 같은 목적지의
  반응형 overflow이며 새 정보 계층이 아니다.
  같은 이름의 링크를 전역 헤더나 Collections에 중복 배치하지 않는다.
- 충분한 본문 폭의 데스크톱은 링크와 한 행이 정확히 40px이다. 하단 경계는 높이를 추가하지 않아
  Vault/Collections 관리 행의 하단과 1px 어긋나지 않는다.
- 모든 링크가 보이고 본문 가용 폭 42rem 이상일 때만 관리 그룹을 오른쪽 끝에 맞춘다.
  뷰포트가 넓어도 사용자가 사이드바를 늘려 본문이 좁아지면 같은 규칙으로
  분리를 해제한다. `VaultShell`의 이름 있는 content container를 기준으로 한다.
- 모바일 링크는 44px이며 내비게이션 자체는 한 줄이다. 링크·아이콘·More의 실제
  너비를 측정하여 남는 항목을 More로 옮긴다. Overview와 현재 목적지를 우선하며
  둘 다 들어가지 않는 매우 좁은 폭에서는 현재 목적지를 우선한다. 나머지는 원래
  순서의 앞부분부터 채우고, 메뉴 안에서도 원래 순서를 유지한다. 뷰어에는 활성
  목적지가 없으므로 Overview와 Search가 우선한다. 폰트 로딩·텍스트 확대·사이드바
  리사이즈에도 재측정하고, 측정용 복제는 비상호작용·접근성 숨김 상태로 격리한다.
- More는 Radix의 nonmodal 메뉴와 실제 링크를 사용한다. 스크롤 잠금을 만들지 않고
  키보드 이동·Escape·새 탭 열기를 유지한다. resize로 포커스된 링크가 숨겨지면 More로,
  More가 사라지면 보이는 링크로 포커스를 옮긴다.
- Rendered / Raw는 중립색 inset 위의 작은 세그먼트형 컨트롤로 변경한다. Vault 메뉴의
  밑줄과 동일한 표현을 쌓지 않는다. 기존 32/44px 조작 높이와 manual-activation tab
  키보드 모델을 유지한다. 새 팔레트·그림자·타이틀·추가 Info 버튼은 도입하지 않는다.
- 좁은 화면의 위치 행과 기존 Vault/Collections 서랍은 유지한다. 저장된 열 폭과
  검색·계정 영역 실측에 따라 임시 서랍으로 전환하고 넓어지면 복원한다.
- 태블릿 헤더는 AKB 로고·이름을 유지하되 긴 제품 보조 문구를 생략한다.
  검색창은 가용 폭에 맞춰 줄고, 프로필 버튼 전체가 화면 안에 남아야 한다.
  데스크톱 전역 검색의 256px 폭은 유지한다.

## 이동·접근성 계약

- 모든 작업 메뉴는 실제 URL을 가진 링크다. 현재 경로와 Vault가 정확히 일치할 때만
  `aria-current="page"`를 부여한다. Overview는 Vault 루트에서만 선택하며,
  별도 Activity 페이지에서 Overview나 다른 항목을 대신 선택하지 않는다.
- Search의 접근성 이름은 `Search this vault`로 전역 검색과 구분한다.
- 문서 제목은 현재 위치를 나타내는 수동적 텍스트이며 제목 클릭 메뉴를 복구하지 않는다.
- 브레드크럼브는 현재 계정·권한 검증·경로에 귀속된 리소스 데이터만 사용한다.
  이전 사용자나 이전 문서 제목이 로딩 중 노출되지 않아야 한다.
- 목록의 현재 Vault 재선택과 브레드크럼브의 Vault 클릭 모두 Overview를 열어
  작업 메뉴를 다시 보여준다. 키보드, 브라우저 Back, 새 탭 링크 동작을 유지한다.
- 새 Vault 링크를 통한 같은 탭 이동은 전체 문서 편집기의 미저장 상태를 확인한다.
  Keep editing / Leave document 확인을 제공하고 기존 로컬 초안 복구를 유지한다.
  저장·이미지 업로드가 진행 중이면 이탈을 막고 이유를 보여주며 Keep editing은
  계속 사용 가능하다. 수정키·새 탭 동작은 현재 편집기를 떠나지 않으므로 가로채지 않는다.
  이 보호는 명시적으로 참여한 Vault 메뉴에 한정되며 전체 Router의 Back/모든 링크
  차단 체계를 새로 도입했다고 주장하지 않는다.
- 검색/Home 미리보기는 로컬 브레드크럼브 + Open in vault + Close를 유지한다.
  전체 뷰어로 승격하면 공통 Vault 메뉴가 나타난다. 모달 내부에는 메뉴를 복제하지
  않으며, 배경 작업공간은 위치를 유지한 채 접근성·상호작용에서 비활성화된다.
  Vault breadcrumb는 여전히 직접 Overview로 이동한다.

## 구현과 검증 범위

- `VaultSectionNavigation`이 기존 여섯 목적지 계약을 사용하며,
  정확한 작업 페이지 및 리소스 뷰어 경로에 공통 내비게이션을 렌더링한다.
- `VaultShell`은 이를 본문 바로 위에 배치한다. Collections는 더 이상
  embedded 모드가 아니며 독립된 `VaultExplorer`를 사용한다.
- 리소스 API·권한·발행 계약은 변경하지 않는다. 읽기 전환 스타일과 새 메뉴의
  미저장 편집 보호만 보완하며 백엔드 변경은 없다.
- 회귀 검증: 데스크톱/모바일 및 light/dark, 메뉴가 유지되는 리소스 화면, 작업 페이지
  이동과 현재 표시, 선택된 Vault 재클릭, 브레드크럼브 복귀, Back,
  Sidebar 접힘·폭 복원, 본문 폭에 따른 관리 그룹 끝 정렬/해제,
  모바일 More와 현재 목적지 우선 노출, 키보드·Escape·resize 포커스, 전체 헤더 버튼 경계.
- 기존 미리보기·편집 초안·발행 권한·표/코드 지역 스크롤 테스트도 유지한다.
- 브라우저 테스트는 격리 HTTP fixture를 사용한다. 실제 계정·사용자 데이터의
  검증이라고 주장하지 않는다. 배포는 로컬 프런트만 하며 백엔드·운영은 변경하지 않는다.

## 참고와 해석

- [GitHub Primer — Navigation](https://primer.style/product/ui-patterns/navigation/):
  위치·목적지·조작의 구분, 관련 콘텐츠 바로 위의 페이지 이동, 부모로 돌아가는 경로.
- [GitHub Primer — UnderlineNav](https://primer.style/product/components/underline-nav/guidelines/):
  관련 콘텐츠 위의 이동, 명확한 라벨, overflow, 여러 밑줄형 탭을 직접 쌓지 않는 원칙.
- [GitHub 실제 파일 뷰어](https://github.com/react/react/blob/main/README.md):
  파일을 열어도 저장소 이동과 개별 파일 조작을 분리해 유지하는 사례.
- [Confluence navigation](https://support.atlassian.com/confluence-cloud/docs/improved-confluence-navigation/):
  전역/공간 탐색을 비교했지만 AKB의 독립 Collections 열을 관리 메뉴로 바꾸는
  근거로 사용하지 않는다.
- `frontend-design`: 구조와 문구가 실제 역할을 나타내도록 하고 중복 장식을 줄인다.
- `ui-ux-pro-max`: 위치 인지, 계층, 키보드·포커스·상태 보존을 검토한다.
  quick-reference의 persistent-nav, navigation-consistency와 계층 구분을 적용한다.
  검색 결과가 이 정확한 배치를 지정하는 것은 아니며 스킬의 일반 원칙을 기존
  독립 Collections 구조에 맞춰 적용했다.

레퍼런스가 AKB의 정확한 배치를 처방하는 것은 아니다. 사용자 피드백과 기존
독립 탐색 열·읽기 흐름에 맞춰 도출한 설계다.
