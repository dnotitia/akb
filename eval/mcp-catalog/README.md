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

OpenRouter provider 설정, DeepSeek primary와 Qwen lightweight model, category별
독립 task 수 2개, 3회 반복, paired task-mean 95% 신뢰구간,
request/time/총비용 hard cap과 token evidence 집계는 `config/run.json`에
사전 등록되어 있다.
반복 결과는 독립 task로 세지 않는다. OpenRouter API key, PAT와
password 값은 환경에서만 읽고 evidence에 쓰지 않는다.

모델은 `deepseek/deepseek-v4-flash-0731`와 `qwen/qwen3.8-27b`로 고정하고,
두 요청 모두 OpenRouter `parasail` upstream만 사용한다. 요청 body에는
`allow_fallbacks=false`, `require_parameters=true`, model별 `max_price`가
강제로 들어가며 model fallback을 의미하는 `models` 배열은 보내지 않는다.
PydanticAI가 MCP schema에 추론해 붙이는 OpenAI strict tool flag는
OpenRouter/Parasail 경로에서 끈다. 44개 전체 tool definition은 유지한다.
등록 가격은 각각 입력/출력 `$0.14/$0.28` 및 `$0.24/$2.20` per million이고,
전체 hard cap은 `$50`이다. 각 trial은 시작 전에 등록한 `$0.10` max-cost
reservation을 잡으며, 전체 manifest의 trial·smoke reservation 합계는 `$36.40`으로
hard cap 아래에 있다. 실제 input/output/total token은 evidence와 secondary metric으로
계속 기록하지만 정상 provider 실행을 중단시키는 누적 token budget gate로 사용하지
않는다. 두 model의 per-response output limit은 full catalog와 terminal response를
수용하도록 `max_tokens=8,192`로 일치시켰다.
`budget_used.wall_seconds`는 병렬 lane duration 합이 아니라 실제 누적 elapsed
wall-clock이며, lane 작업량은 별도의 `model_work_seconds`로 기록한다.

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
stdout에 제공한 ready JSON이어야 한다. `app-control-plane` benchmark runtime은
model×transport별 isolated child descriptor 네 개를 `benchmark_cells`에 함께
제공하며, benchmark가 runtime을 시작하거나 warm하지 않는다. runtime supervisor와
같은 stdin handoff를 사용할 때는 `--descriptor -`를 쓴다.

### Runtime credential handoff

orchestrator는 기존 `mcp-stdio-runtime`의 ready descriptor를 benchmark
command의 stdin으로 전달하고, benchmark process에 다음 두 environment name만
secure handoff로 주입해야 한다. 값은 descriptor, argv, 로그, 파일과 evidence를
통과하지 않는다.

runtime는 benchmark manifest와 같은 `app-control-plane` scenario로
기동한다(`CRABBOX_RUNTIME_SCENARIO=app-control-plane`). orchestrator의
기존 descriptor-derived `env_names` forwarding에 두 이름을 포함시키고,
runtime 안의 benchmark process에서 실행한다. serving runtime 자체는
OpenRouter 값을 사용하지 않는다.

```text
MCP_BENCH_OPENROUTER_BASE_URL
MCP_BENCH_OPENROUTER_API_KEY
```

`MCP_BENCH_OPENROUTER_BASE_URL`은 반드시
`https://openrouter.ai/api/v1`이어야 한다. runtime의 AKB PAT 환경은 MCP
server 연결용으로만 사용하며 OpenRouter credential과 섞지 않는다. 아래
명령은 기존 runtime helper의 remote `exec`가 descriptor stdin과 두 env 값을
주입한 뒤 runtime checkout에서 실행한다.

```bash
# The existing runtime exec supplies the descriptor JSON on stdin.
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench validate --descriptor -
```

### Source revision handoff

runtime source identity는 명시된 `AKB_E2E_SOURCE_REVISION`이 있으면 그것을
우선 사용한다. 값은 lowercase 40-hex Git SHA여야 하며, 값이 없을 때만 실제
checkout의 `git rev-parse HEAD`를 사용한다. raw sync처럼 `.git`이 없는
checkout에서 값이 없거나 형식이 틀리면 runtime은 resource를 만들기 전에
`blocked_runtime_config`로 종료하며 `unknown`을 descriptor/discovery에
기록하지 않는다. 이 변수는 credential이 아니므로 provider/PAT handoff와
분리된 runtime identity input이다.

## Baseline과 candidate 실행

먼저 동일 corpus와 manifest로 baseline을 한 번 실행하고, candidate runtime을
같은 scenario·reset 계약으로 실행한다. 두 descriptor는 서로 다른
candidate-bound runtime에서 올 수 있지만, run artifact의 corpus hash,
protocol, repeat, model/settings, fixture reset 계약은 동일해야 한다.

```bash
# The orchestrator injects the two OpenRouter values and the scoped PAT into
# the process environment; no credential value belongs in this command.
export MCP_BENCH_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
: "${MCP_BENCH_OPENROUTER_API_KEY:?securely injected by orchestrator}"
: "${MCP_BENCH_READ_ONLY_PAT:?securely injected by orchestrator}"

# The descriptor's AKB_E2E_USERNAME / AKB_E2E_PASSWORD are injected by the
# runtime handoff and are used only to mint a fresh PAT after each reset.

uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench run --arm baseline \
  --descriptor - \
  --output /private/run/baseline.json \
  --checkpoint /private/run/baseline.checkpoint.json

uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench run --arm candidate \
  --descriptor - \
  --output /private/run/candidate.json \
  --checkpoint /private/run/candidate.checkpoint.json

uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench compare \
  --baseline /private/run/baseline.json \
  --candidate /private/run/candidate.json \
  --output /private/run/comparison.json
```

실제 provider key나 scoped PAT가 없으면 `run`은 catalog나 model 호출을
시작하기 전에 `needs_user_input`으로 종료한다. descriptor의 credential
환경변수 이름만 저장되며 값은 argv, log, report, trace에 남지 않는다.
per-trial reset으로 PAT 저장소가 재생성되므로 descriptor의 username/password
환경도 reset 후 fresh PAT 발급에 필요하다. default profile은 기존 full PAT
scope로, `read_only` profile은 기존 `read` scope로 새 token을 발급하며,
재발급 경로가 없으면 stale PAT를 재사용하지 않고 fail-closed로 종료한다.

`--checkpoint`를 지정하면 각 trial 직후 redacted JSON을 같은
디렉터리에 임시 파일로 fsync한 뒤 atomic replace한다. 재개는 동일한
`--resume /private/run/baseline.checkpoint.json`으로 실행하며, source revision,
run manifest hash, task corpus hash, arm, model, transport, task, repeat index가
모두 일치하고 provider usage/cost 및 routing 증거가 유효한 completed trial만 재사용한다.
사용량·비용과 양쪽 state 관측이 확보된 request/output limit 또는 tool/terminal
행동 실패도 `success=false`인 completed trial로 재사용하며 다시 실행하지 않는다.
증거가 없는 provider/인프라 실패와 미완료 trial만 다시 실행하고,
손상·입력 불일치·secret 포함 checkpoint는 첫 provider 호출 전에 fail-closed한다.
checkpoint에는 이전 시도의 실제 usage/cost도 누적해 재개가 `$50` cap을 우회하지
않도록 한다.

full paid run 전에는 baseline/candidate arm 각각 primary/lightweight × HTTP/stdio의
네 cell smoke gate를 실행한다. 각 cell은 전체 unfiltered toolset을 붙인 실제
model request를 보내고, 성공한 MCP tool call을 최소 하나 수행한 뒤 그 결과를
받은 follow-up terminal model response와 positive usage, Parasail routing evidence를
얻어야 한다. 하나라도 pre-response failure, zero usage, token limit, tool call
실패 또는 terminal response 누락이면 full trial을 시작하지 않는다. smoke는 최소
multi-turn provider/tool 계약을 검증하는 gate이며 full corpus의 최대 token 수를
추정하는 calibration이 아니다. 이미 passing checkpoint가 있으면 smoke 결과도
재사용한다.

각 trial의 reset은 reset endpoint 응답만으로 완료 처리하지 않는다. repository가
선언한 app/fixture health가 모두 `status=ready`이고 fixture scenario가 일치할
때까지 기다린 뒤 state observation과 provider execution을 시작한다. reset 또는
PAT cleanup이 실패하면 primary failure stage를 유지하고, 이미 완료된 trial과
`budget_used`를 포함한 redacted `status=incomplete` artifact를 먼저 기록한 뒤
non-zero로 종료한다. 불완전 artifact는 `compare` 입력으로 허용하지 않는다.
일반 HTTP 요청 timeout은 30초로 유지하고, repository runtime의
`DEFAULT_TIMEOUT_SECONDS`와 맞춘 reset/readiness budget 180초를 별도로 적용한다.
provider/toolset 단계에서 model request, usage/cost 또는 state evidence를 얻지
못한 실패는 `status=incomplete` artifact와 failed checkpoint로 남기며,
zero-request arm을 성공 baseline으로 취급하지 않는다. 반대로 실제 provider
usage/cost와 양쪽 state 관측을 얻은 request/output limit 또는 tool/terminal 행동
실패는 측정된 unsuccessful trial로 charge·metric·checkpoint에 남긴다. 오류
evidence는 redacted exception chain, HTTP status와 `failure_kind`(`provider`,
`output_limit`, `request_limit`, `terminal_response`, `tool`, `budget`)를 구분해
보존한다. 429와 usage/cost 누락은 provider failure로 분류하고 hidden
retry/fallback 없이 resume에서만 다시 실행한다.

runtime supervisor의 fixture reset은 PostgreSQL/MinIO Compose dependency
container, network, volume과 backend/embed/stdio process identity를 내리지 않고
유지한다. PostgreSQL application rows, MinIO object, Git fixture data만
in-place로 비운 뒤 seed하고, benchmark의 네 cell은 서로 다른 runtime/Compose
project로 격리된다. reset 전후 identity가 달라지면 reset은 실패하며, reset
count/time과 identity 보존 결과를 evidence에 기록한다. 인접 trial 사이에는
teardown reset을 중복 실행하지 않고 다음 setup이 하나의 reset boundary를
소유한다.

artifact와 checkpoint의 timing에는 resume 전후 모든 attempt의 누적
`end_to_end_wall_seconds`와 provisioning, credential, catalog capture,
fixture reset, provider-wait, model execution, checkpoint, 기타 overhead breakdown이
포함된다. OpenRouter 429/rate-limit 구간은 provider failure로 보존하면서
`provider_wait`에 별도 배정한다.
겹치는 병렬 구간은 한 번만 wall time에 배정되며, breakdown 합은 해당 attempt의
실제 elapsed wall time과 일치한다.

## Evidence

각 run artifact에는 다음이 들어간다.

- runtime이 제공한 exact source revision, backend/proxy artifact version,
  protocol, scenario, reset body와 capability profile
- transport·credential profile별 실제 unfiltered `tools/list`, tool count,
  canonical catalog hash와 UTF-8 byte/4 token estimate
- model class/id/version/settings, Pydantic Evals report, 반복별 raw model
  arguments와 server-facing arguments, tool 결과·오류·usage·latency·cost
- final response, fixture before/after state와 결정적 state checks
- end-to-end wall-clock, checkpoint new/reused/rerun count, fixture reset count/time,
  PostgreSQL/MinIO dependency identity preservation, four-cell smoke의 successful
  MCP call/follow-up terminal response 결과 및
  재현 가능한 `artifact_hash_input`/`artifact_hash`

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
