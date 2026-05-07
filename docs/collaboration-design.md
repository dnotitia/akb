# 에이전트 협업 시나리오 및 권한 설계

## 1. 실제 협업 시나리오

### 시나리오 A: 팀 세팅
```
[한] "engineering vault 만들어줘"
  → akb_create_vault("engineering", description="백엔드 엔지니어링 KB")
  → 한이 owner가 됨

[한] "김영로, 이수진에게 write 권한 줘"
  → akb_grant(vault="engineering", user="youngro", role="writer")
  → akb_grant(vault="engineering", user="sujin", role="writer")

[한] "product 팀에게는 읽기만 가능하게"
  → akb_grant(vault="engineering", user="product-park", role="reader")
```

### 시나리오 B: 일상 협업
```
[영로 에이전트] "내가 접근 가능한 vault 뭐 있어?"
  → akb_list_vaults()
  → [{"name":"engineering", "role":"writer"}, {"name":"company-wide", "role":"reader"}]

[영로 에이전트] "engineering에 API 리뷰 결과 저장해줘"
  → akb_put(vault="engineering", ..., content="## 리뷰 결과\n\n...")
  → created_by: "youngro"

[한 에이전트] "최근에 engineering에서 누가 뭐 했어?"
  → akb_recent(vault="engineering")
  → "youngro가 api-review.md 생성 (2시간 전)"

[한 에이전트] "engineering vault에 누가 접근 가능해?"
  → akb_vault_members(vault="engineering")
  → [{"user":"han", "role":"owner"}, {"user":"youngro", "role":"writer"}, ...]
```

### 시나리오 C: 지식 발견
```
[수진 에이전트] "gRPC 관련 문서 있어?"
  → akb_search(query="gRPC")  ← 접근 가능한 vault만 검색됨
  → engineering/decisions/adr-grpc.md (score: 0.85)

[수진 에이전트] "이 문서와 연결된 다른 문서들은?"
  → akb_graph(vault="engineering", doc_id="...", depth=2)
  → nodes: [adr-grpc, migration-plan, payment-api-spec], edges: [...]
```

### 시나리오 D: 접근 요청
```
[외부 팀원 에이전트] "infrastructure vault 접근하고 싶어"
  → akb_list_vaults()
  → infrastructure가 안 보임 (private)

[외부 팀원] → Slack으로 vault owner에게 요청
[owner 에이전트] → akb_grant(vault="infrastructure", user="external-kim", role="reader")
```

### 시나리오 E: Vault 관리
```
[한 에이전트] "engineering vault 정보 보여줘"
  → akb_vault_info(vault="engineering")
  → {owner: "han", members: 5, documents: 42, last_activity: "2h ago"}

[한 에이전트] "q1-project vault는 아카이브해"
  → akb_archive_vault(vault="q1-project")
  → vault status → archived (읽기만 가능)
```

### 시나리오 F: 유저/팀 탐색
```
[한 에이전트] "김영로 계정 찾아줘"
  → akb_search_users(query="youngro")
  → [{"username":"youngro", "display_name":"김영로", "email":"youngro@..."}]

[한 에이전트] "현재 등록된 사용자 누구야?"
  → akb_search_users()
  → 전체 사용자 목록
```

---

## 2. 권한 모델

### 2.1 Vault Role 체계

| Role | Vault 발견 | 읽기 | 쓰기 | 멤버 관리 | Vault 설정 |
|------|----------|------|------|---------|----------|
| **owner** | O | O | O | O | O |
| **admin** | O | O | O | O | X |
| **writer** | O | O | O | X | X |
| **reader** | O | O | X | X | X |
| (none) | X | X | X | X | X |

- Vault 생성자가 자동으로 owner
- owner는 양도 가능 (transfer)
- 권한 없는 사용자에게는 vault이 목록에서 안 보임

### 2.2 PAT Scope 체계

현재: `scopes: ["read", "write"]` (글로벌)

고도화:
```
scopes:
  - "vaults:read"      # vault 목록 조회
  - "vaults:write"     # vault 생성
  - "documents:read"   # 문서 읽기
  - "documents:write"  # 문서 쓰기
  - "members:read"     # 멤버 목록 조회
  - "members:write"    # 멤버 grant/revoke
  - "users:read"       # 유저 검색
  - "admin"            # 모든 권한
```

실제 접근 판단 = PAT scope ∩ Vault role:
- PAT에 "documents:write" scope가 있어도, vault에서 reader role이면 쓰기 불가
- PAT에 "documents:read" scope만 있으면, vault에서 writer role이어도 쓰기 불가

### 2.3 DB 변경

```sql
-- vault_access 테이블 수정
vault_access (
  vault_id UUID REFERENCES vaults,
  user_id UUID REFERENCES users,      -- principal → user_id로 변경
  role TEXT NOT NULL,                  -- owner, admin, writer, reader
  granted_by UUID REFERENCES users,
  granted_at TIMESTAMPTZ,
  UNIQUE(vault_id, user_id)
)
```

---

## 3. MCP Tool Set (전체)

### 기존 (유지, 권한 필터링 추가)
| Tool | 변경 사항 |
|------|---------|
| `akb_list_vaults` | 접근 가능한 vault만 반환 + role 표시 |
| `akb_browse` | vault 접근 권한 체크 |
| `akb_search` | 접근 가능한 vault만 검색 |
| `akb_put` | writer 이상만 가능 |
| `akb_update` | writer 이상만 가능 |
| `akb_delete` | writer 이상만 가능 |
| `akb_get` | reader 이상만 가능 |

### 새로 추가
| Tool | 용도 | 필요 권한 |
|------|------|---------|
| `akb_vault_info` | vault 상세 정보 (owner, 멤버 수, 문서 수) | reader |
| `akb_grant` | 사용자에게 vault 접근 권한 부여 | owner/admin |
| `akb_revoke` | 접근 권한 제거 | owner/admin |
| `akb_vault_members` | vault 멤버 목록 + role | reader |
| `akb_search_users` | 사용자 검색 | 인증된 사용자 |
| `akb_transfer_ownership` | vault 소유권 양도 | owner |
