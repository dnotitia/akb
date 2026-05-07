# Git/Markdown 기반 업무 도구 벤치마크 리서치

## 핵심 발견

**Git 네이티브 + 팀 협업 + AI의 교차점이 비어있음.** Wiki.js, Gollum은 Git 네이티브이나 AI/RAG가 없고, RAG 도구들은 Git 네이티브가 아님. AKB가 이 교차점을 점유.

---

## 1. Git 기반 위키/지식베이스 (Confluence 대체)

| 프로젝트 | Stars | Git 네이티브 | 실시간 협업 | AI/RAG | 핵심 |
|---------|-------|------------|----------|--------|------|
| **Wiki.js** | 28.1K | O (양방향 동기화) | X | X | 가장 깊은 Git 연동 위키. v3에서 PR 워크플로우 예정 |
| **Outline** | 37.9K | X (DB) | O | X | 가장 폴리쉬된 UI/UX. BSL 라이선스 |
| **BookStack** | 18.6K | X | X | X | 비기술자 친화적. MIT |
| **Docmost** | 19.7K | X | O | X | Confluence 스타일 Space. 빠른 성장 |
| **Gollum** | 14.2K | O (순수 Git) | X | X | GitHub Wiki 기반 기술. 투박한 UI |
| **GitBook** | 상용 | O (GitHub 동기화) | X | O (AI 검색) | 개발자 문서 특화. SaaS 전환 |

## 2. Git 기반 이슈 트래킹 (Jira 대체)

| 프로젝트 | Stars | 핵심 | 한계 |
|---------|-------|------|------|
| **git-bug** | 9.7K | Git 내부 객체로 이슈 저장. `git push/pull`로 동기화 | PM 기능 없음. 비기술자 사용 불가 |
| **git-issue** | - | Shell 기반. Git 브랜치 내 파일로 이슈 관리 | 웹 UI 없음 |
| **Sciit** | - | 코드 주석 = 이슈. Git hook 자동 추적 | 코드 기반 프로젝트에만 적용 |
| **TrackDown** | - | 단일 MD 파일에 이슈 관리 | 대규모 부적합 |

## 3. Markdown 기반 작업 관리 / Notion 대체

| 프로젝트 | Stars | 핵심 | Git | 한계 |
|---------|-------|------|-----|------|
| **AppFlowy** | 68.9K | Notion 대체 1위. Rust + Flutter | X | Git 아님. DB 기반 |
| **AFFiNE** | 66.9K | Notion+Miro 결합. CRDT 실시간 | X | Git 아님 |
| **Obsidian** | 클로즈드 | 로컬 MD PKM. 500+ 플러그인 | 플러그인 | 개인용. 팀 사용 시 conflict |
| **Logseq** | 41.8K | 블록 단위 링크. 아웃라이너+그래프 | X | 대규모 성능 이슈 |
| **SilverBullet** | 4.9K | "Markdown as Platform". Lua 확장 | X | 개인용 |
| **Anytype** | 7.3K | P2P 암호화. 데이터 주권 최강 | X | 자체 포맷 |

## 4. AI/에이전트 네이티브 지식 관리

| 프로젝트 | 핵심 | 한계 |
|---------|------|------|
| **rag-agent** | MD→벡터 DB→LLM Q&A 최단 경로 | 프로토타입. 조직 규모 부적합 |
| **markdown-kb** | GitHub MD→시맨틱 검색→RAG | ASP.NET. 소규모 |
| **knowledge_agent** | 에이전트가 자율적으로 KB 관리/갱신 | 실험적 |
| **AI-KM** (논문) | MD→임베딩→RAG 학술적 검증 | 논문 수준 |

## 5. Docs as Code / Everything as Code

| 프로젝트 | Stars | 핵심 | 용도 |
|---------|-------|------|------|
| **Backstage TechDocs** | 33.0K | 개발자 포털 + MkDocs 문서. CNCF | 개발자 문서 |
| **MkDocs Material** | 26.4K | 가장 아름다운 기술 문서 테마 | 정적 문서 |
| **Docusaurus** | 64.4K | React + MDX. Meta 유지보수 | 외부 문서 |
| **Dendron** | 7.4K | VS Code 계층적 MD. 스키마 시스템 | 개발자 PKM |
| **Foam** | 17.0K | VS Code + GitHub. Roam 스타일 | 개인 PKM |

## 6. 올인원 (Jira+Confluence+Slack 통합)

| 프로젝트 | Stars | 핵심 |
|---------|-------|------|
| **Huly** | 25.2K | 이슈+문서+채팅+HR+CRM 통합 |
| **Plane** | 47.3K | Jira/Linear 대체 1위. 문서 기능 포함 |
| **Tegon** | 1.9K | Dev-first Jira 대체. AI 자동화 |

## 7. 학술 연구 / 성공 사례

- **"Decentralized Collaborative Knowledge Management using Git"** (WWW 2019) — Git으로 RDF 데이터셋 분산 협업
- **Squarespace Engineering** — Docs-as-Code 전환. MD + Backstage TechDocs + PR 리뷰
- **UK Home Office** — 정부 기관 Docs as Code 표준 채택

## 8. 인컴번트 대체 실패 원인

1. **비기술자 진입 장벽** — Git/MD은 개발자 최적화
2. **실시간 협업 부재** — Git은 비동기 모델
3. **구조화 데이터 한계** — Jira 커스텀 필드/워크플로우 표현 어려움
4. **검색/발견성** — 파일시스템 검색 < DB 전문 검색
5. **권한 관리** — Git repo 단위 ≠ 페이지 단위 세분화
6. **네트워크 효과** — Atlassian 생태계 통합
7. **마이그레이션 비용** — 전환 비용 > 이익

---

## 9. AKB 포지셔닝

### 비어있는 교차점

```
Git 네이티브    ✓ (Wiki.js, Gollum 수준)
+ 팀 협업       ✓ (Vault ACL, 멀티유저)
+ AI/RAG       ✓ (벡터 검색, 에이전트 MCP)
+ 문서 그래프    ✓ (명시적 링크 기반)
+ 에이전트 네이티브 ✓ (MCP 24 tools)
```

**이 조합을 가진 프로젝트는 없음.**

### 가장 가까운 경쟁자

- **SilverBullet** — MD 플랫폼 + 쿼리. 하지만 AI 없고 개인용
- **Wiki.js** — Git 양방향 동기화. 하지만 AI/KG 없음
- **Dendron** — 계층 구조 + Git. 하지만 VS Code 종속, 유지보수 저하

### AKB가 해결하는 문제

| 기존 실패 원인 | AKB 해결 방식 |
|-------------|-------------|
| 비기술자 진입 장벽 | 에이전트가 대신 문서 생성/검색. 사람은 에이전트를 통해 간접 사용 |
| 실시간 협업 부재 | 에이전트 간 비동기 협업이 기본 모델. 실시간 필요 없음 |
| 검색 열위 | 벡터 검색 + 트리 리트리벌로 DB 수준 이상의 검색 |
| 권한 세분화 | Multi-repo Vault + Vault 내 ACL |
