#!/bin/bash
# Bundle the code + docs for a codebase audit (e.g. ChatGPT).
# EXPLICITLY EXCLUDES all .env files (secrets) — see NET-25 in
# NETWORK-SPEED-AUDIT-2026-08-21.md; a prior manual zip leaked
# .env and .env.bak into an audit archive.
#
# Usage:
#   bash scripts/bundle_codebase_for_audit.sh [name-suffix]
#
# Output: ~/Desktop/receptionist-codebase-YYYY-MM-DD_HHMM.zip

set -euo pipefail

REPO="/Users/az/Desktop/Receptionist Agent"
STAMP=$(date +%Y-%m-%d_%H%M)
SUFFIX="${1:-lean}"
OUT="/Users/az/Desktop/receptionist-codebase-${STAMP}-${SUFFIX}.zip"

echo "Bundling codebase from $REPO"
echo "Output: $OUT"
echo

# Excludes — critical, keep in sync with NET-25 policy.
# Rule of thumb: never include any file that could contain a secret.
EXCLUDES=(
    # Secrets — never include
    "*.env"
    "*.env.*"
    "*/.env"
    "*/.env.*"
    # 2026-08-25: .claude/settings.local.json contains permission
    # rules with real bearer tokens baked into curl command patterns
    # (Vapi private keys spotted during audit-2026-08-25 bundle).
    # Same class of leak as .env: user-scoped state that carries
    # credentials.  Never bundle.
    "*/.claude/*"
    ".claude/*"
    # 2026-08-25: SSH keys (Lightsail .pem sits in repo root). A prior
    # bundle leaked LightsailDefaultKey-us-east-1.pem — anyone with the
    # zip could SSH into prod. Match both cwd and any subdir.
    "*.pem"
    "*.key"
    "*.p12"
    "*.pfx"
    "*id_rsa*"
    "*id_ed25519*"
    "*.ssh/*"
    # Except the example (safe, no secret values)
    # NB: zip supports include-after-exclude via a second pass; simpler
    # to just list explicit `-x` patterns below.

    # Local state / caches / generated data
    "*/node_modules/*"
    "node_modules/*"
    "*/__pycache__/*"
    "__pycache__/*"
    "*/.pytest_cache/*"
    ".pytest_cache/*"
    "*/.mypy_cache/*"
    ".mypy_cache/*"
    "*/.venv/*"
    ".venv/*"
    "*.pyc"
    "*/data/logs/*"
    "*/data/calls/*"
    "*/data/rag/*"
    "*/data/tts_cache/*"
    "*/data/pipeline.db*"
    "*/data/voiceops.db*"
    "*/data/consent.db*"
    # 2026-08-23: prior bundle was 521 MB from these — audio samples,
    # ONNX model checkpoints, generated audio outputs. None are code.
    "*.wav"
    "*.mp3"
    "*.m4a"
    "*.ogg"
    "*.onnx"
    "*.bin"
    "*.pt"
    "*.pth"
    "*.ckpt"
    "*.safetensors"
    "*/output/*"
    "output/*"
    "*/checkpoints/*"
    "checkpoints/*"
    "*/data/models/*"
    "data/models/*"
    "*/data/chatterbox_clone/*"
    "data/chatterbox_clone/*"
    "*/data/local_calls/*"
    "data/local_calls/*"
    "*/data/mlx_shootout/*"
    "data/mlx_shootout/*"
    "*/apps/api/data/call_logs_archive/*"
    "apps/api/data/call_logs_archive/*"
    "*/apps/api/data/rag/*"
    "apps/api/data/rag/*"
    "*.db"
    "*.db-wal"
    "*.db-shm"
    "*.sqlite"
    "*.sqlite-*"
    # Research/audit artifacts already-shipped (avoid recursive size bloat)
    "*/audit-bundle.zip"

    # Git
    "*/.git/*"
    ".git/*"

    # OS
    "*/.DS_Store"
    ".DS_Store"

    # Editor
    "*/.idea/*"
    "*/.vscode/*"

    # Local audit artifacts (they'd be circular)
    "*/receptionist-codebase-*.zip"
    "*/research-bundle-*.zip"
)

# Build zip -x arg list
ZIP_EXCLUDES=()
for pat in "${EXCLUDES[@]}"; do
    ZIP_EXCLUDES+=(-x "$pat")
done

# Zip
cd "$REPO"
rm -f "$OUT"
zip -r -q "$OUT" . "${ZIP_EXCLUDES[@]}"

# Force-include .env.example so reviewers see the expected variable list
# without any values.
if [ -f "$REPO/.env.example" ]; then
    zip -q "$OUT" ".env.example"
fi

# Sanity check: assert no .env leaked
LEAKED=$(unzip -l "$OUT" | awk '{print $4}' | grep -E '(^|/)\.env($|\.)' | grep -v '\.env\.example$' || true)
if [ -n "$LEAKED" ]; then
    echo
    echo "!!! SECURITY: .env leaked into archive:"
    echo "$LEAKED"
    echo "Aborting — remove the file and re-run."
    rm -f "$OUT"
    exit 1
fi

# 2026-08-25: assert no SSH/TLS private keys leaked. Added after a
# LightsailDefaultKey-us-east-1.pem slipped through — that key gives
# SSH access to prod.
LEAKED_KEYS=$(unzip -l "$OUT" | awk '{print $4}' | grep -Ei '\.(pem|key|p12|pfx)$|id_rsa|id_ed25519' || true)
if [ -n "$LEAKED_KEYS" ]; then
    echo
    echo "!!! SECURITY: private key material leaked into archive:"
    echo "$LEAKED_KEYS"
    echo "Aborting — remove the file(s) and re-run."
    rm -f "$OUT"
    exit 1
fi

# 2026-08-25: content-scan for API-key-shaped strings that slipped
# INSIDE ANY bundled file.  Added after .claude/settings.local.json
# leaked a live Vapi bearer token inside baked-in curl commands
# (permission rules).  Same class of leak whether the credential
# lives in .env or in an app-state JSON.
#
# Patterns cover the common bearer/key formats we use.  False
# positives are fine — the script exits so a human eyeballs the hit.
LEAKED_TOKENS=$(
    unzip -p "$OUT" 2>/dev/null | grep -EIo \
        -e 'Bearer [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
        -e 'sk-[A-Za-z0-9]{20,}' \
        -e 'sk-proj-[A-Za-z0-9_-]{20,}' \
        -e 'gsk_[A-Za-z0-9]{40,}' \
        -e 'pat_[A-Za-z0-9]{20,}' \
        -e 'xoxb-[0-9A-Za-z-]{20,}' \
        -e 'AIza[0-9A-Za-z_-]{35}' \
        -e 'AKIA[0-9A-Z]{16}' \
        -e 'SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}' \
        2>/dev/null | sort -u || true
)
if [ -n "$LEAKED_TOKENS" ]; then
    echo
    echo "!!! SECURITY: credential-shaped strings found inside bundled files:"
    echo "$LEAKED_TOKENS" | head -20
    echo
    echo "Aborting — inspect the bundle, add the offending path to EXCLUDES,"
    echo "and re-run.  If it's a false positive (example placeholder), consider"
    echo "adding an allowlist here."
    rm -f "$OUT"
    exit 1
fi

echo
echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "Files: $(unzip -l "$OUT" | tail -1 | awk '{print $2}')"
echo
echo "First 25 entries:"
unzip -l "$OUT" | head -30
