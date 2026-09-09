# MCP tool catalog benchmark

이 디렉터리는 AKB의 baseline/candidate MCP catalog를 같은 source-blind
task corpus와 resettable fixture로 비교하는 독립 실행 환경이다. 기존
`eval/agentic-bench`의 arm filtering이나 ReAct loop를 사용하지 않는다.

## 고정된 실행 계약

- MCP protocol은 `2026-07-28`로 고정한다.
- 실행기는 PydanticAI `2.41.0`의 `Agent`와 `MCPToolset`, FastMCP Client
  `4.0.3`의 공식 Streamable HTTP/stdio transport를 사용한다.
- Pydantic Evals `2.41.0`의 `Dataset.evaluate`, `repeat`,
  `CaseLifecycle`, case evaluator와 report evaluator가 반복·reset·채점·보고를
  담당한다.
- `MCPToolset`에는 server가 반환한 전체 `tools/list`만 전달한다. filtering,
  lazy loading, built-in coding tool, 자동 tool retry는 끈다.
- `corpus/tasks.json`의 prompt에는 실제 tool 이름이나 MCP method가 없다.
  `operation_map`은 prompt 밖의 scorer control-plane에서만 logical operation을
  분류한다.
- `fixture`의 state probe는 최종 상태와 destructive 보호를 결정적으로
  검사한다. before/after 관찰이 없으면 안전성·성공을 통과시키지 않는다.

모델·provider 설정, category별 독립 task 수 2개, 3회 반복, paired task-mean
95% 신뢰구간, request/token/time/총비용 상한은 `config/run.json`에 사전
등록되어 있다. 반복 결과는 독립 task로 세지 않는다. 모델 API key, PAT와
password 값은 환경에서만 읽고 evidence에 쓰지 않는다.

## 독립 환경 설치와 계약 확인

```bash
uv sync --locked --extra dev --project eval/mcp-catalog
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench validate
```

실제 runtime descriptor까지 정적으로 확인하려면 다음처럼 경로를 추가한다.

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench validate \
  --descriptor /private/run/descriptor.json
```

descriptor는 repository-owned schema-v2 `serve --profile transport-proxy`가
stdout에 제공한 ready JSON이어야 한다. benchmark가 runtime을 시작하거나
warm하지 않는다.

## Baseline과 candidate 실행

먼저 동일 corpus와 manifest로 baseline을 한 번 실행하고, candidate runtime을
같은 scenario·reset 계약으로 실행한다. 두 descriptor는 서로 다른
candidate-bound runtime에서 올 수 있지만, run artifact의 corpus hash,
protocol, repeat, model/settings, fixture reset 계약은 동일해야 한다.

```bash
export MCP_BENCH_OPENAI_BASE_URL=https://api.openai.com/v1
export MCP_BENCH_OPENAI_API_KEY='(secret supplied by the operator)'
export MCP_BENCH_READ_ONLY_PAT='(scoped secret supplied by the operator)'

uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench run --arm baseline \
  --descriptor /private/run/baseline-descriptor.json \
  --output /private/run/baseline.json

uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench run --arm candidate \
  --descriptor /private/run/candidate-descriptor.json \
  --output /private/run/candidate.json

uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench compare \
  --baseline /private/run/baseline.json \
  --candidate /private/run/candidate.json \
  --output /private/run/comparison.json
```

실제 provider key나 scoped PAT가 없으면 `run`은 catalog나 model 호출을
시작하기 전에 `needs_user_input`으로 종료한다. descriptor의 credential
환경변수 이름만 저장되며 값은 argv, log, report, trace에 남지 않는다.

## Evidence

각 run artifact에는 다음이 들어간다.

- runtime이 제공한 exact source revision, backend/proxy artifact version,
  protocol, scenario, reset body와 capability profile
- transport·credential profile별 실제 unfiltered `tools/list`, tool count,
  canonical catalog hash와 UTF-8 byte/4 token estimate
- model class/id/version/settings, Pydantic Evals report, 반복별 raw model
  arguments와 server-facing arguments, tool 결과·오류·usage·latency·cost
- final response, fixture before/after state와 결정적 state checks

comparison은 task별 반복 평균을 만든 뒤 paired difference의 단측 95% 하한을
계산한다. 통과에는 안전성 regression 0, success 하한 `-3%p` 이상,
destructive/auth category regression 0, catalog token 50% 이상 감소,
first-action/argument error 악화 없음, token 또는 latency 개선이 모두
필요하다. 표본이 부족하거나 artifact가 짝지어지지 않으면 `inconclusive`다.

## 개발자 확인

```bash
uv run --locked --project eval/mcp-catalog ruff check mcp_catalog tests
uv run --locked --project eval/mcp-catalog mypy --python-version 3.14 mcp_catalog
uv run --locked --project eval/mcp-catalog --extra dev --extra server pytest -q
```

저장소 전체 contributor gate는 repository root에서 `bash scripts/check.sh`로
실행한다.
