#!/usr/bin/env bash
# Launch CPU/Edge chat inference with llama.cpp server
set -euo pipefail

MODEL_PATH=${1:-"./models/qwen2.5-14b-instruct-q4_k_m.gguf"}
PORT=${2:-8080}
THREADS=${3:-8}
CTX_SIZE=${4:-4096}

echo "Starting llama-server (chat) with model: ${MODEL_PATH} on port: ${PORT}..."

exec llama-server \
    --model "${MODEL_PATH}" \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --threads "${THREADS}" \
    --ctx-size "${CTX_SIZE}" \
    --n-gpu-layers 0 \
    --embedding false
