#!/usr/bin/env bash
# Install Higgs Audio v3 TTS on the WSL Ubuntu side of the 2x3090 PC.
#
# Copy this file to the PC via scp, then run:
#     ssh pc@192.168.1.100
#     wsl
#     bash /mnt/c/Users/pc/pc_setup_higgs_wsl.sh
#
# What it does:
#   1. Verifies GPUs + CUDA
#   2. Creates a virtualenv at ~/higgs-env
#   3. Installs PyTorch (CUDA 12) + vLLM
#   4. Downloads Higgs Audio v3 TTS model (~8 GB)
#   5. Starts vLLM server on port 8001
#   6. Prints test curl for you to run from anywhere on the LAN
#
# Idempotent — safe to re-run if any step fails.

set -euo pipefail

VENV=~/higgs-env
MODEL="bosonai/higgs-audio-v3-tts-4b"
PORT=8001

echo "=========================================="
echo "Higgs Audio v3 TTS setup on WSL"
echo "=========================================="

echo ""
echo "[1/6] Verifying GPUs..."
nvidia-smi --query-gpu=index,name,memory.total --format=csv || {
    echo "FATAL: nvidia-smi failed. Make sure NVIDIA driver + CUDA are installed on the WSL side."
    exit 1
}

echo ""
echo "[2/6] Verifying Python + CUDA toolchain..."
python3 --version
nvcc --version | tail -3

echo ""
echo "[3/6] Creating virtualenv at $VENV..."
if [ ! -d "$VENV" ]; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-venv python3-pip
    python3 -m venv "$VENV"
    echo "  ✓ created $VENV"
else
    echo "  ✓ already exists"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip

echo ""
echo "[4/6] Installing PyTorch (CUDA 12.1) + vLLM..."
# Torch first, matching CUDA 12.x on this box
pip install --quiet torch --index-url https://download.pytorch.org/whl/cu121
# vLLM (this pulls in a lot; ~5-15 min)
pip install --quiet vllm
echo "  ✓ vllm version:"
python -c "import vllm; print('   ', vllm.__version__)"

echo ""
echo "[5/6] Downloading Higgs Audio v3 TTS model..."
# Hugging Face auth NOT required for boson models — they're public
pip install --quiet huggingface_hub
huggingface-cli download "$MODEL" --local-dir ~/models/higgs-audio-v3-tts-4b || {
    echo "  Model download failed; try manually:"
    echo "    huggingface-cli download $MODEL --local-dir ~/models/higgs-audio-v3-tts-4b"
    exit 1
}
echo "  ✓ model downloaded"

echo ""
echo "[6/6] Starting vLLM server on port $PORT..."
echo "  Log will stream to ~/higgs-server.log"
echo "  To kill later:  pkill -f 'vllm serve'"
echo ""

# Launch in background so we can print instructions
nohup vllm serve "$MODEL" \
    --port "$PORT" \
    --host 0.0.0.0 \
    --tensor-parallel-size 1 \
    > ~/higgs-server.log 2>&1 &

sleep 5
echo "  ✓ vllm serve PID: $!"
echo ""
echo "=========================================="
echo "Setup complete!"
echo "=========================================="
echo ""
echo "Next steps (do these on the Mac side):"
echo ""
echo "1. On Windows PowerShell (Admin), forward port so LAN can reach WSL:"
echo "   netsh interface portproxy add v4tov4 listenport=$PORT listenaddress=0.0.0.0 connectport=$PORT connectaddress=\$(wsl -e bash -c 'hostname -I | cut -d\" \" -f1')"
echo "   netsh advfirewall firewall add rule name=\"WSL Higgs $PORT\" dir=in action=allow protocol=TCP localport=$PORT"
echo ""
echo "2. From the Mac, test:"
echo "   curl http://192.168.1.100:$PORT/v1/models"
echo ""
echo "3. Watch server logs:"
echo "   tail -f ~/higgs-server.log"
