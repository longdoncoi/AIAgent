#!/usr/bin/env bash
# Launch text embeddings service with llama.cpp server
set -euo pipefail

MODEL_PATH=${1:-"./models/bge-m3-q8_0.gguf"}
PORT=${2:-8081}
THREADS=${3:-4}

echo "Starting llama-server (embedding) with model: ${MODEL_PATH} on port: ${PORT}..."

exec llama-server \
    --model "${MODEL_PATH}" \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --threads "${THREADS}" \
    --embedding \
    --pooling cls
