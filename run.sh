#!/bin/bash

gpu_model=$(nvidia-smi --query-gpu=gpu_name --format=csv,noheader | head -n1)
model_number=$(echo "$gpu_model" | grep -oE '[0-9]+' | tail -1)

if [[ $model_number == 40* ]]; then
    export DITTO_DATA_ROOT="/app/weights/checkpoints/ditto_trt_40xx"
    echo "Detected 4000-series GPU. Setting DITTO_DATA_ROOT to $DITTO_DATA_ROOT"
elif [[ $model_number =~ ^[0-9]{4}$ ]]; then
    export DITTO_DATA_ROOT="/app/weights/checkpoints/ditto_trt_$model_number"
    echo "Detected GPU model $model_number. Setting DITTO_DATA_ROOT to $DITTO_DATA_ROOT"
else
    echo "Error: Unknown GPU model '$gpu_model'"
    exit 1
fi

echo "Starting Render"
python3 /app/server.py &

echo "Starting WebRTC service"
sleep 30 && uv run simple_webrtc_server.py --host 0.0.0.0 --port 8080
