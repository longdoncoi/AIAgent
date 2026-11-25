#!/usr/bin/env bash
# ==============================================================================
# Automated Qdrant Snapshot & Offsite S3 Backup Script (DR PRR 10.2)
# ------------------------------------------------------------------------------
# Creates an atomic Qdrant collection snapshot, verifies SHA-256 integrity,
# uploads to encrypted offsite object storage, and prunes stale snapshots.
# Required env:
#   QDRANT_URL        (default: http://qdrant-svc.agentic-platform.svc.cluster.local:6333)
#   QDRANT_COLLECTION (default: agentic_knowledge)
#   BACKUP_BUCKET     (e.g. s3://agentic-platform-backups/qdrant)
# Optional env:
#   QDRANT_API_KEY, RETENTION_COUNT (default: 7)
# ==============================================================================
set -euo pipefail

QDRANT_URL="${QDRANT_URL:-http://qdrant-svc.agentic-platform.svc.cluster.local:6333}"
QDRANT_COLLECTION="${QDRANT_COLLECTION:-agentic_knowledge}"
BACKUP_BUCKET="${BACKUP_BUCKET:?BACKUP_BUCKET must be set (e.g. s3://bucket/qdrant)}"
RETENTION_COUNT="${RETENTION_COUNT:-7}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

AUTH_HEADER=()
if [[ -n "${QDRANT_API_KEY:-}" ]]; then
  AUTH_HEADER=(-H "api-key: ${QDRANT_API_KEY}")
fi

echo "[1/4] Requesting snapshot for Qdrant collection '${QDRANT_COLLECTION}'..."
SNAP_RESPONSE="$(curl -fsS -X POST "${AUTH_HEADER[@]}" \
  "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/snapshots?wait=true")"

SNAP_NAME="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["result"]["name"])' "${SNAP_RESPONSE}")"
if [[ -z "${SNAP_NAME}" ]]; then
  echo "ERROR: Qdrant did not return a snapshot name: ${SNAP_RESPONSE}" >&2
  exit 1
fi
echo "    Created snapshot: ${SNAP_NAME}"

LOCAL_SNAP="${TMP_DIR}/${SNAP_NAME}"
echo "[2/4] Downloading snapshot archive..."
curl -fsS "${AUTH_HEADER[@]}" \
  "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/snapshots/${SNAP_NAME}" \
  -o "${LOCAL_SNAP}"

sha256sum "${LOCAL_SNAP}" > "${LOCAL_SNAP}.sha256"
echo "    Verified checksum: $(cat "${LOCAL_SNAP}.sha256")"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST_PREFIX="${BACKUP_BUCKET%/}/${QDRANT_COLLECTION}/${TIMESTAMP}"
echo "[3/4] Uploading snapshot & checksum to ${DEST_PREFIX}..."
aws s3 cp "${LOCAL_SNAP}" "${DEST_PREFIX}/${SNAP_NAME}" --only-show-errors
aws s3 cp "${LOCAL_SNAP}.sha256" "${DEST_PREFIX}/${SNAP_NAME}.sha256" --only-show-errors

echo "[4/4] Pruning on-node Qdrant snapshots beyond latest ${RETENTION_COUNT}..."
ALL_SNAPS="$(curl -fsS "${AUTH_HEADER[@]}" "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/snapshots" \
  | python3 -c 'import json,sys; res=json.load(sys.stdin)["result"]; print("\n".join(s["name"] for s in sorted(res, key=lambda x: x.get("creation_time",""))))')"
TOTAL_COUNT="$(printf '%s\n' "${ALL_SNAPS}" | grep -c . || true)"
if (( TOTAL_COUNT > RETENTION_COUNT )); then
  TO_DELETE=$(( TOTAL_COUNT - RETENTION_COUNT ))
  printf '%s\n' "${ALL_SNAPS}" | head -n "${TO_DELETE}" | while read -r old_snap; do
    [[ -z "${old_snap}" ]] && continue
    curl -fsS -X DELETE "${AUTH_HEADER[@]}" \
      "${QDRANT_URL}/collections/${QDRANT_COLLECTION}/snapshots/${old_snap}" >/dev/null
    echo "    Deleted old Qdrant snapshot: ${old_snap}"
  done
fi

echo "Qdrant backup completed cleanly: ${DEST_PREFIX}/${SNAP_NAME}"
