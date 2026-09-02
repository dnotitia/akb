#!/usr/bin/env bash
# Host-side validation shared by Kustomize and Helm installers. It must run
# before image build, namespace creation, or chart installation.

set -euo pipefail

SECRET_MODE="${SECRET_MODE:-manual}"
SECRET_PROFILE="${SECRET_PROFILE:-development}"
SECRET_SEAL_MODE="${SECRET_SEAL_MODE:-plaintext}"
SECRET_KEY_SHARES="${SECRET_KEY_SHARES:-5}"
SECRET_KEY_THRESHOLD="${SECRET_KEY_THRESHOLD:-3}"
SECRET_PGP_KEYS="${SECRET_PGP_KEYS:-}"
SECRET_ROOT_TOKEN_PGP_KEY="${SECRET_ROOT_TOKEN_PGP_KEY:-}"
SECRET_STORE_SEAL_CONFIG_SECRET="${SECRET_STORE_SEAL_CONFIG_SECRET:-}"

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
if [[ "${SECRET_SEAL_MODE}" != "plaintext" &&
      ( "${SECRET_MODE}" != "bundled" || "${SECRET_PROFILE}" != "production" ) ]]; then
  echo "SECRET_SEAL_MODE=${SECRET_SEAL_MODE} requires a bundled production Secret Manager profile" >&2
  exit 2
fi
if [[ "${SECRET_SEAL_MODE}" == "pgp" ]]; then
  if [[ -z "${SECRET_PGP_KEYS}" || -z "${SECRET_ROOT_TOKEN_PGP_KEY}" ]]; then
    echo "PGP mode requires SECRET_PGP_KEYS and SECRET_ROOT_TOKEN_PGP_KEY before deployment" >&2
    exit 2
  fi
  pgp_key_count=0
  old_ifs="${IFS}"
  IFS=','
  for pgp_key_ref in ${SECRET_PGP_KEYS}; do
    IFS="${old_ifs}"
    pgp_key_ref="${pgp_key_ref#${pgp_key_ref%%[![:space:]]*}}"
    pgp_key_ref="${pgp_key_ref%${pgp_key_ref##*[![:space:]]}}"
    [[ -n "${pgp_key_ref}" ]] || continue
    if [[ "${pgp_key_ref}" != keybase:* && ! -s "${pgp_key_ref}" ]]; then
      echo "PGP public key does not exist or is empty: ${pgp_key_ref}" >&2
      exit 2
    fi
    pgp_key_count=$((pgp_key_count + 1))
  done
  IFS="${old_ifs}"
  if (( pgp_key_count != SECRET_KEY_SHARES )); then
    echo "SECRET_PGP_KEYS requires ${SECRET_KEY_SHARES} comma-separated public keys; received ${pgp_key_count}" >&2
    exit 2
  fi
  if [[ "${SECRET_ROOT_TOKEN_PGP_KEY}" != keybase:* &&
        ! -s "${SECRET_ROOT_TOKEN_PGP_KEY}" ]]; then
    echo "Bootstrap root-token PGP public key does not exist or is empty: ${SECRET_ROOT_TOKEN_PGP_KEY}" >&2
    exit 2
  fi
fi
if [[ "${SECRET_SEAL_MODE}" == "auto" &&
      ! "${SECRET_STORE_SEAL_CONFIG_SECRET}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
  echo "Auto Seal mode requires SECRET_STORE_SEAL_CONFIG_SECRET before deployment" >&2
  exit 2
fi
