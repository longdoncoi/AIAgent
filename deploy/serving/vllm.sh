#!/usr/bin/env bash
# Launch high-throughput GPU inference with vLLM
set -euo pipefail

MODEL_ID=${1:-"Qwen/Qwen2.5-72B-Instruct"}
PORT=${2:-8001}
GPU_MEMORY_UTILIZATION=${3:-0.90}
MAX_MODEL_LEN=${4:-32768}
TENSOR_PARALLEL_SIZE=${5:-4}
API_KEY=${6:-${VLLM_API_KEY:-"sk-vllm-dummy"}}

echo "Starting vLLM server with model: ${MODEL_ID} on port: ${PORT} (TP=${TENSOR_PARALLEL_SIZE})..."

exec python3 -m vllm.entrypoints.openai.api_server \
    --model "${MODEL_ID}" \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}" \
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --enable-auto-tool-choice \
    --tool-call-parser hermes \
    --api-key "${API_KEY}" \
    --disable-log-requests
