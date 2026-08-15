#!/usr/bin/env bash
# Token Firewall v3: setup
# Works on Android (Termux), Linux, macOS

set -e
cd "$(dirname "$0")"

echo "=== Token Firewall v3 Setup ==="

# Install deps
if command -v pip &>/dev/null; then
    pip install fastapi "uvicorn[standard]" --break-system-packages 2>/dev/null || \
    pip install fastapi "uvicorn[standard]"
elif command -v pip3 &>/dev/null; then
    pip3 install fastapi "uvicorn[standard]" --break-system-packages 2>/dev/null || \
    pip3 install fastapi "uvicorn[standard]"
fi

# Android extras
if [ -d "/data/data/com.termux" ]; then
    echo "Android detected, checking Termux packages..."
    pkg install -y android-tools termux-api 2>/dev/null || true
fi

# Create env file if missing
if [ ! -f ~/.token-firewall.env ]; then
cat > ~/.token-firewall.env << 'ENV'
# Token Firewall v3: Environment Config
# Copy this and fill in your values

export TF_PLATFORM=android         # auto-detected if not set

# LLM: primary provider
export TF_LLM_BASE_URL=https://integrate.api.nvidia.com/v1
export TF_LLM_API_KEY=your-api-key-here
export TF_LLM_MODEL=moonshotai/kimi-k2.5

# LLM fallback chain (semicolon separated: url|key|model)
# IMPORTANT: must be single-quoted to prevent shell interpreting | as pipe
export TF_LLM_FALLBACKS='https://api.cerebras.ai/v1|your-cerebras-key|gpt-oss-120b'

# Server
export TF_HOST=127.0.0.1
export TF_PORT=8000

# Tuning (optional)
# export TF_FUZZY_THRESHOLD=0.45
# export TF_STALE_DAYS=30
# export TF_DISABLE_ADB=false
# export TF_DISABLE_TERMUX=false
ENV
    echo "Created ~/.token-firewall.env, fill in your API keys"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Start with:"
echo "  source ~/.token-firewall.env && python server.py"
echo ""
echo "Or use the auto-restart wrapper:"
echo "  bash start.sh"
