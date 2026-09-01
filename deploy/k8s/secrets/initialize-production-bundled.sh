#!/usr/bin/env bash
# Initialize, unseal, and bootstrap a production bundled Vault-compatible
# cluster without inventing an AKB-specific recovery-key format.

set -euo pipefail

: "${NAMESPACE:?}"
: "${SECRET_ENGINE:?}"
: "${SECRET_STORE_POD:?}"
: "${SECRET_STORE_STATEFULSET:?}"
: "${BACKEND_IMAGE:?}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
AUTH_PROFILE="${AUTH_PROFILE:-local}"
SECRET_SEAL_MODE="${SECRET_SEAL_MODE:-plaintext}"
SECRET_KEY_SHARES="${SECRET_KEY_SHARES:-5}"
SECRET_KEY_THRESHOLD="${SECRET_KEY_THRESHOLD:-3}"
SECRET_PGP_KEYS="${SECRET_PGP_KEYS:-}"
SECRET_ROOT_TOKEN_PGP_KEY="${SECRET_ROOT_TOKEN_PGP_KEY:-}"
SECRET_RECOVERY_PGP_KEYS="${SECRET_RECOVERY_PGP_KEYS:-}"
KV_MOUNT="${KV_MOUNT:-kv}"
KV_PATH="${KV_PATH:-akb/runtime}"
AUTH_MOUNT="${KUBERNETES_AUTH_MOUNT:-kubernetes}"
VAULT_ROLE="${VAULT_ROLE:-akb-runtime-reader}"
OPERATOR_ROLE="${SECRET_OPERATOR_ROLE:-akb-operator-admin}"
OPERATOR_SERVICE_ACCOUNT="${SECRET_OPERATOR_SERVICE_ACCOUNT:-akb-secret-admin}"
BOOTSTRAP_RECEIPT="akb-secret-manager-bootstrap"

KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

case "${SECRET_ENGINE}" in
  openbao)
    CLI="bao"
    ADDR_ENV="BAO_ADDR"
    CACERT_ENV="BAO_CACERT"
    TLS_SERVER_NAME_ENV="BAO_TLS_SERVER_NAME"
    CACERT_FILE="/openbao/tls/ca.crt"
    ;;
  hashicorp-vault)
    CLI="vault"
    ADDR_ENV="VAULT_ADDR"
    CACERT_ENV="VAULT_CACERT"
    TLS_SERVER_NAME_ENV="VAULT_TLS_SERVER_NAME"
    CACERT_FILE="/vault/tls/ca.crt"
    ;;
  *)
    echo "Unsupported production Secret Manager: ${SECRET_ENGINE}" >&2
    exit 2
    ;;
esac

if [[ "${SECRET_SEAL_MODE}" != "plaintext" &&
      "${SECRET_SEAL_MODE}" != "pgp" &&
      "${SECRET_SEAL_MODE}" != "auto" ]]; then
  echo "SECRET_SEAL_MODE must be plaintext, pgp, or auto" >&2
  exit 2
fi
if [[ ! "${SECRET_KEY_SHARES}" =~ ^[1-9][0-9]*$ ]] ||
   [[ ! "${SECRET_KEY_THRESHOLD}" =~ ^[1-9][0-9]*$ ]] ||
   (( SECRET_KEY_THRESHOLD > SECRET_KEY_SHARES )); then
  echo "SECRET_KEY_SHARES and SECRET_KEY_THRESHOLD must form a valid quorum" >&2
  exit 2
fi
if [[ ! "${OPERATOR_ROLE}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] ||
   [[ ! "${OPERATOR_SERVICE_ACCOUNT}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "Secret Manager operator role or ServiceAccount name is invalid" >&2
  exit 2
fi

LOCAL_ADDRESS="https://127.0.0.1:8200"
PGP_REMOTE_DIR="/tmp/akb-init-pgp.$$"

cleanup() {
  "${KUBECTL[@]}" exec -n "${NAMESPACE}" "${SECRET_STORE_POD}" -- \
    rm -rf "${PGP_REMOTE_DIR}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cli_on() {
  local pod="$1"
  local tls_server_name
  shift
  tls_server_name="${pod}.${SECRET_STORE_STATEFULSET}-internal.${NAMESPACE}.svc"
  "${KUBECTL[@]}" exec -i -n "${NAMESPACE}" "${pod}" -- \
    env "${ADDR_ENV}=${LOCAL_ADDRESS}" "${CACERT_ENV}=${CACERT_FILE}" \
    "${TLS_SERVER_NAME_ENV}=${tls_server_name}" \
    "${CLI}" "$@"
}

status_json() {
  local pod="$1"
  local output rc
  set +e
  output="$(cli_on "${pod}" status -format=json 2>/dev/null)"
  rc=$?
  set -e
  if [[ ${rc} -ne 0 && ${rc} -ne 2 ]]; then
    echo "Unable to read seal status from ${pod}" >&2
    return 1
  fi
  printf '%s' "${output}"
}

wait_for_pod_running() {
  local pod="$1"
  local attempt phase
  for attempt in $(seq 1 150); do
    phase="$("${KUBECTL[@]}" get pod "${pod}" -n "${NAMESPACE}" \
      -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [[ "${phase}" == "Running" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${pod} to enter Running" >&2
  return 1
}

wait_for_status() {
  local pod="$1"
  local attempt status
  for attempt in $(seq 1 150); do
    status="$(status_json "${pod}" 2>/dev/null || true)"
    if jq -e '.initialized == true or .initialized == false' \
      <<<"${status}" >/dev/null 2>&1; then
      printf '%s' "${status}"
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${pod} seal status endpoint" >&2
  return 1
}

wait_for_initialized() {
  local pod="$1"
  local attempt status initialized
  for attempt in $(seq 1 150); do
    status="$(status_json "${pod}" 2>/dev/null || true)"
    initialized="$(jq -r '.initialized // false' <<<"${status}" 2>/dev/null || true)"
    if [[ "${initialized}" == "true" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${pod} to join the initialized Raft cluster" >&2
  return 1
}

wait_for_unsealed() {
  local pod="$1"
  local attempt status
  for attempt in $(seq 1 300); do
    status="$(status_json "${pod}" 2>/dev/null || true)"
    if jq -e '.sealed == false' <<<"${status}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${pod} to become unsealed" >&2
  return 1
}

require_interactive_tty() {
  if [[ ! -t 0 || ! -t 1 || ! -e /dev/tty ]]; then
    echo "Production ${SECRET_SEAL_MODE} initialization requires a trusted interactive TTY." >&2
    echo "Do not send operator init output to CI, session-recorded shells, or deployment logs." >&2
    return 1
  fi
}

tty_print() {
  printf '%s\n' "$*" >/dev/tty
}

confirm_handoff() {
  local confirmation
  tty_print ""
  tty_print "Store the Unseal/Recovery Shares in the approved Password Manager or offline custody location."
  tty_print "Keep the bootstrap-only root value available until installation succeeds; do not retain it as day-2 admin authority."
  tty_print "Type STORED to continue. The installer does not create a durable local key file."
  IFS= read -r confirmation </dev/tty
  if [[ "${confirmation}" != "STORED" ]]; then
    echo "Initialization output handoff was not confirmed" >&2
    exit 3
  fi
}

upload_public_key() {
  local source="$1"
  local remote_name="$2"
  if [[ "${source}" == keybase:* ]]; then
    printf '%s' "${source}"
    return
  fi
  if [[ ! -s "${source}" ]]; then
    echo "PGP public key does not exist or is empty: ${source}" >&2
    exit 2
  fi
  "${KUBECTL[@]}" exec -n "${NAMESPACE}" "${SECRET_STORE_POD}" -- \
    sh -ec "umask 077; mkdir -p '${PGP_REMOTE_DIR}'"
  if head -n 1 "${source}" | grep -q '^-----BEGIN PGP PUBLIC KEY BLOCK-----'; then
    # Vault/OpenBao accept binary or base64-encoded OpenPGP packets, not the
    # ASCII-armour headers/checksum. Normalize common `.asc` exports without
    # requiring the install host to carry a private-key-capable GPG runtime.
    awk '
      /^-----BEGIN PGP PUBLIC KEY BLOCK-----$/ { in_block = 1; next }
      in_block && !body && /^[[:space:]]*$/ { body = 1; next }
      body && /^=/ { exit }
      body && /^-----END PGP PUBLIC KEY BLOCK-----$/ { exit }
      body { gsub(/\r/, ""); printf "%s", $0 }
    ' "${source}" | \
      "${KUBECTL[@]}" exec -i -n "${NAMESPACE}" "${SECRET_STORE_POD}" -- \
      tee "${PGP_REMOTE_DIR}/${remote_name}" >/dev/null
  else
    "${KUBECTL[@]}" exec -i -n "${NAMESPACE}" "${SECRET_STORE_POD}" -- \
      tee "${PGP_REMOTE_DIR}/${remote_name}" >/dev/null <"${source}"
  fi
  "${KUBECTL[@]}" exec -n "${NAMESPACE}" "${SECRET_STORE_POD}" -- \
    chmod 600 "${PGP_REMOTE_DIR}/${remote_name}"
  printf '%s' "${PGP_REMOTE_DIR}/${remote_name}"
}

upload_public_key_list() {
  local value="$1"
  local prefix="$2"
  local expected="$3"
  local item remote joined="" count=0
  local old_ifs="${IFS}"
  IFS=','
  for item in ${value}; do
    IFS="${old_ifs}"
    item="${item#${item%%[![:space:]]*}}"
    item="${item%${item##*[![:space:]]}}"
    [[ -n "${item}" ]] || continue
    remote="$(upload_public_key "${item}" "${prefix}-${count}.asc")"
    if [[ -n "${joined}" ]]; then
      joined+=","
    fi
    joined+="${remote}"
    count=$((count + 1))
    IFS=','
  done
  IFS="${old_ifs}"
  if [[ ${count} -ne ${expected} ]]; then
    echo "${prefix} requires ${expected} comma-separated PGP public keys; received ${count}" >&2
    exit 2
  fi
  printf '%s' "${joined}"
}

print_init_output() {
  local init_json="$1"
  local label="$2"
  local root_token
  tty_print ""
  tty_print "=== OFFICIAL ${SECRET_ENGINE} INITIALIZATION OUTPUT ==="
  jq -r --arg label "${label}" \
    '.[$label] // [] | to_entries[] | "\($label) \(.key + 1): \(.value)"' \
    <<<"${init_json}" >/dev/tty
  root_token="$(jq -r '.root_token // empty' <<<"${init_json}")"
  if [[ -n "${root_token}" ]]; then
    tty_print "Initial Root Token (bootstrap-only; revoked on success): ${root_token}"
  fi
  tty_print "=== END INITIALIZATION OUTPUT ==="
}

unseal_with_keys() {
  local pod="$1"
  shift
  local key result
  for key in "$@"; do
    # The official CLIs deliberately reject a key piped into a non-TTY and
    # placing the share in argv exposes it to process inspection. Send the
    # native sys/unseal JSON request through stdin instead; the share remains
    # transient in this installer process and the server-side request body.
    result="$(printf '%s' "${key}" | \
      cli_on "${pod}" write -format=json sys/unseal key=-)"
    if jq -e '(.data.sealed == false) or (.sealed == false)' \
      <<<"${result}" >/dev/null; then
      return 0
    fi
  done
  echo "Unseal threshold was not reached for ${pod}" >&2
  return 1
}

prompt_key_holders_for_pod() {
  local pod="$1"
  local status sealed
  status="$(status_json "${pod}")"
  sealed="$(jq -r '.sealed' <<<"${status}")"
  if [[ "${sealed}" == "false" ]]; then
    return
  fi
  tty_print ""
  tty_print "AwaitingKeyHolderUnseal: ${pod}"
  tty_print "Each of ${SECRET_KEY_THRESHOLD} key holders decrypts their assigned share and runs:"
  tty_print "  kubectl${KUBE_CONTEXT:+ --context ${KUBE_CONTEXT}} exec -it -n ${NAMESPACE} ${pod} -- env ${ADDR_ENV}=${LOCAL_ADDRESS} ${CACERT_ENV}=${CACERT_FILE} ${TLS_SERVER_NAME_ENV}=${pod}.${SECRET_STORE_STATEFULSET}-internal.${NAMESPACE}.svc ${CLI} operator unseal"
  tty_print "Press Enter after ${pod} reports Sealed: false."
  IFS= read -r _ </dev/tty
  status="$(status_json "${pod}")"
  if [[ "$(jq -r '.sealed' <<<"${status}")" != "false" ]]; then
    echo "${pod} is still sealed" >&2
    exit 3
  fi
}

prompt_root_token() {
  local token
  tty_print "Decrypt the Initial Root Token on the bootstrap administrator's secure terminal."
  printf 'Initial Root Token (hidden): ' >/dev/tty
  IFS= read -r -s token </dev/tty
  printf '\n' >/dev/tty
  if [[ -z "${token}" ]]; then
    echo "Initial Root Token is required to complete bootstrap" >&2
    exit 3
  fi
  printf '%s' "${token}"
}

apply_receipt() {
  local cluster_id="$1"
  local seal_type="$2"
  "${KUBECTL[@]}" create configmap "${BOOTSTRAP_RECEIPT}" -n "${NAMESPACE}" \
    --from-literal=contract=akb-secret-manager-bootstrap-v1 \
    --from-literal=cluster-id="${cluster_id}" \
    --from-literal=engine="${SECRET_ENGINE}" \
    --from-literal=seal-type="${seal_type}" \
    --from-literal=seal-mode="${SECRET_SEAL_MODE}" \
    --from-literal=auth-profile="${AUTH_PROFILE}" \
    --from-literal=kv-path="${KV_MOUNT}/${KV_PATH}" \
    --from-literal=operator-role="${OPERATOR_ROLE}" \
    --from-literal=operator-service-account="${OPERATOR_SERVICE_ACCOUNT}" \
    --from-literal=root-token-revoked=true \
    --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f - >/dev/null
}

replicas="$("${KUBECTL[@]}" get statefulset "${SECRET_STORE_STATEFULSET}" \
  -n "${NAMESPACE}" -o jsonpath='{.spec.replicas}')"
if [[ ! "${replicas}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Unable to determine Secret Manager replica count" >&2
  exit 1
fi

wait_for_pod_running "${SECRET_STORE_POD}"
leader_status="$(wait_for_status "${SECRET_STORE_POD}")"
initialized="$(jq -r '.initialized' <<<"${leader_status}")"
seal_type="$(jq -r '.type // "unknown"' <<<"${leader_status}")"
receipt_exists=false
if "${KUBECTL[@]}" get configmap "${BOOTSTRAP_RECEIPT}" -n "${NAMESPACE}" >/dev/null 2>&1; then
  receipt_exists=true
  receipt_engine="$("${KUBECTL[@]}" get configmap "${BOOTSTRAP_RECEIPT}" \
    -n "${NAMESPACE}" -o jsonpath='{.data.engine}')"
  receipt_auth_profile="$("${KUBECTL[@]}" get configmap "${BOOTSTRAP_RECEIPT}" \
    -n "${NAMESPACE}" -o jsonpath='{.data.auth-profile}')"
  receipt_seal_mode="$("${KUBECTL[@]}" get configmap "${BOOTSTRAP_RECEIPT}" \
    -n "${NAMESPACE}" -o jsonpath='{.data.seal-mode}')"
  receipt_kv_path="$("${KUBECTL[@]}" get configmap "${BOOTSTRAP_RECEIPT}" \
    -n "${NAMESPACE}" -o jsonpath='{.data.kv-path}')"
  receipt_operator_role="$("${KUBECTL[@]}" get configmap "${BOOTSTRAP_RECEIPT}" \
    -n "${NAMESPACE}" -o jsonpath='{.data.operator-role}')"
  receipt_operator_service_account="$("${KUBECTL[@]}" get configmap "${BOOTSTRAP_RECEIPT}" \
    -n "${NAMESPACE}" -o jsonpath='{.data.operator-service-account}')"
  if [[ "${receipt_engine}" != "${SECRET_ENGINE}" ||
        "${receipt_auth_profile}" != "${AUTH_PROFILE}" ||
        "${receipt_seal_mode}" != "${SECRET_SEAL_MODE}" ||
        "${receipt_kv_path}" != "${KV_MOUNT}/${KV_PATH}" ||
        "${receipt_operator_role}" != "${OPERATOR_ROLE}" ||
        "${receipt_operator_service_account}" != "${OPERATOR_SERVICE_ACCOUNT}" ]]; then
    echo "Requested profile conflicts with the existing Secret Manager bootstrap receipt" >&2
    echo "Refusing to reinterpret an initialized cluster or its Secret Contract" >&2
    exit 2
  fi
fi

if [[ "${initialized}" == "true" ]]; then
  if [[ "${SECRET_SEAL_MODE}" == "auto" && "${seal_type}" == "shamir" ]]; then
    echo "Existing cluster uses Shamir but SECRET_SEAL_MODE=auto was requested" >&2
    exit 2
  fi
  if [[ "${SECRET_SEAL_MODE}" != "auto" && "${seal_type}" != "shamir" ]]; then
    echo "Existing cluster uses ${seal_type} Auto Seal but ${SECRET_SEAL_MODE} was requested" >&2
    exit 2
  fi
fi

root_token=""
unseal_keys=()

if [[ "${initialized}" != "true" ]]; then
  require_interactive_tty
  init_args=(operator init -format=json)
  display_label="unseal_keys_b64"

  case "${SECRET_SEAL_MODE}" in
    plaintext)
      init_args+=(
        -key-shares="${SECRET_KEY_SHARES}"
        -key-threshold="${SECRET_KEY_THRESHOLD}"
      )
      ;;
    pgp)
      if [[ -z "${SECRET_PGP_KEYS}" || -z "${SECRET_ROOT_TOKEN_PGP_KEY}" ]]; then
        echo "PGP mode requires SECRET_PGP_KEYS and SECRET_ROOT_TOKEN_PGP_KEY" >&2
        exit 2
      fi
      remote_pgp_keys="$(upload_public_key_list \
        "${SECRET_PGP_KEYS}" "unseal-key" "${SECRET_KEY_SHARES}")"
      remote_root_key="$(upload_public_key \
        "${SECRET_ROOT_TOKEN_PGP_KEY}" "root-token.asc")"
      init_args+=(
        -key-shares="${SECRET_KEY_SHARES}"
        -key-threshold="${SECRET_KEY_THRESHOLD}"
        -pgp-keys="${remote_pgp_keys}"
        -root-token-pgp-key="${remote_root_key}"
      )
      ;;
    auto)
      if [[ "${seal_type}" == "shamir" || "${seal_type}" == "unknown" ]]; then
        echo "SECRET_SEAL_MODE=auto requires a working non-Shamir seal in SECRET_STORE_EXTRA_VALUES" >&2
        exit 2
      fi
      init_args+=(
        -recovery-shares="${SECRET_KEY_SHARES}"
        -recovery-threshold="${SECRET_KEY_THRESHOLD}"
      )
      display_label="recovery_keys_b64"
      if [[ -n "${SECRET_RECOVERY_PGP_KEYS}" ]]; then
        remote_recovery_keys="$(upload_public_key_list \
          "${SECRET_RECOVERY_PGP_KEYS}" "recovery-key" "${SECRET_KEY_SHARES}")"
        init_args+=( -recovery-pgp-keys="${remote_recovery_keys}" )
      fi
      if [[ -n "${SECRET_ROOT_TOKEN_PGP_KEY}" ]]; then
        remote_root_key="$(upload_public_key \
          "${SECRET_ROOT_TOKEN_PGP_KEY}" "root-token.asc")"
        init_args+=( -root-token-pgp-key="${remote_root_key}" )
      fi
      ;;
  esac

  init_json="$(cli_on "${SECRET_STORE_POD}" "${init_args[@]}")"
  print_init_output "${init_json}" "${display_label}"
  confirm_handoff

  if [[ "${SECRET_SEAL_MODE}" == "plaintext" ]]; then
    while IFS= read -r key; do
      unseal_keys+=("${key}")
    done < <(jq -r '.unseal_keys_b64[]' <<<"${init_json}")
    root_token="$(jq -r '.root_token' <<<"${init_json}")"
    unseal_with_keys "${SECRET_STORE_POD}" "${unseal_keys[@]}"
  elif [[ "${SECRET_SEAL_MODE}" == "pgp" ]]; then
    prompt_key_holders_for_pod "${SECRET_STORE_POD}"
    root_token="$(prompt_root_token)"
  else
    wait_for_unsealed "${SECRET_STORE_POD}"
    if [[ -n "${SECRET_ROOT_TOKEN_PGP_KEY}" ]]; then
      root_token="$(prompt_root_token)"
    else
      root_token="$(jq -r '.root_token' <<<"${init_json}")"
    fi
  fi
else
  if [[ "${receipt_exists}" == "true" ]]; then
    echo "Secret Manager bootstrap receipt already exists; preserving initialized state"
  else
    require_interactive_tty
  fi
fi

# Every Raft member is a separate server process. retry_join enrolls followers;
# Shamir still requires the threshold on each process, whereas Auto Seal does
# not require human input after the provider becomes available.
for index in $(seq 0 $((replicas - 1))); do
  pod="${SECRET_STORE_STATEFULSET}-${index}"
  wait_for_pod_running "${pod}"
  wait_for_initialized "${pod}"
  pod_status="$(status_json "${pod}")"
  if [[ "$(jq -r '.sealed' <<<"${pod_status}")" == "true" ]]; then
    if [[ "${SECRET_SEAL_MODE}" == "auto" || "$(jq -r '.type' <<<"${pod_status}")" != "shamir" ]]; then
      wait_for_unsealed "${pod}"
    elif (( ${#unseal_keys[@]} > 0 )); then
      unseal_with_keys "${pod}" "${unseal_keys[@]}"
    else
      require_interactive_tty
      prompt_key_holders_for_pod "${pod}"
    fi
  fi
done

if [[ "${receipt_exists}" != "true" ]]; then
  if [[ -z "${root_token}" ]]; then
    root_token="$(prompt_root_token)"
  fi
  printf '%s\n' "${root_token}" | \
    NAMESPACE="${NAMESPACE}" KUBE_CONTEXT="${KUBE_CONTEXT}" \
    SECRET_ENGINE="${SECRET_ENGINE}" \
    SECRET_STORE_POD="${SECRET_STORE_POD}" \
    SECRET_STORE_LOCAL_ADDRESS="${LOCAL_ADDRESS}" \
    SECRET_STORE_TLS_SERVER_NAME="${SECRET_STORE_POD}.${SECRET_STORE_STATEFULSET}-internal.${NAMESPACE}.svc" \
    SECRET_STORE_CACERT="${CACERT_FILE}" \
    BACKEND_IMAGE="${BACKEND_IMAGE}" KV_MOUNT="${KV_MOUNT}" KV_PATH="${KV_PATH}" \
    AUTH_PROFILE="${AUTH_PROFILE}" KUBERNETES_AUTH_MOUNT="${AUTH_MOUNT}" \
    VAULT_ROLE="${VAULT_ROLE}" REVOKE_ROOT_TOKEN=true \
    SECRET_OPERATOR_ROLE="${OPERATOR_ROLE}" \
    SECRET_OPERATOR_SERVICE_ACCOUNT="${OPERATOR_SERVICE_ACCOUNT}" \
    SECRET_OPERATOR_TOKEN_TTL="${SECRET_OPERATOR_TOKEN_TTL:-30m}" \
    SECRET_OPERATOR_TOKEN_MAX_TTL="${SECRET_OPERATOR_TOKEN_MAX_TTL:-4h}" \
    bash "${SCRIPT_DIR}/bootstrap-bundled.sh"
  root_token=""
  leader_status="$(status_json "${SECRET_STORE_POD}")"
  apply_receipt \
    "$(jq -r '.cluster_id' <<<"${leader_status}")" \
    "$(jq -r '.type' <<<"${leader_status}")"
  echo "Secret Manager bootstrap receipt recorded; initial root token is revoked"
fi

for index in $(seq 0 $((replicas - 1))); do
  pod="${SECRET_STORE_STATEFULSET}-${index}"
  wait_for_unsealed "${pod}"
done
