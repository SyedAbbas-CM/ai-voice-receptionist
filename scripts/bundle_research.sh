#!/bin/bash
# Bundle every research/audit/plan doc into ONE zip for ChatGPT synthesis.
# See docs/RESEARCH-BUNDLE-INDEX-2026-08-20.md for the read order + synthesis brief.
#
# Usage:
#   bash scripts/bundle_research.sh
#
# Output: ~/Desktop/research-bundle-YYYY-MM-DD.zip

set -euo pipefail

REPO="/Users/az/Desktop/Receptionist Agent"
STAMP=$(date +%Y-%m-%d)
STAGING=$(mktemp -d)
OUT="/Users/az/Desktop/research-bundle-${STAMP}.zip"

echo "staging in $STAGING"

# Function: copy file into staging with a numbered prefix so read-order is
# obvious in a directory listing.
copy() {
    local prefix="$1"
    local src="$2"
    if [ ! -f "$src" ]; then
        echo "  SKIP (missing): $src"
        return
    fi
    local base
    base=$(basename "$src")
    cp "$src" "$STAGING/${prefix}_${base}"
    echo "  + ${prefix}_${base}"
}

# 1. Foundation
copy "01_FOUNDATION" "$REPO/WORKING-NOTES.md"
copy "02_FOUNDATION" "$REPO/docs/UNIFIED-IMPLEMENTATION-PLAN.md"
copy "03_INDEX"      "$REPO/docs/RESEARCH-BUNDLE-INDEX-2026-08-20.md"

# 2. Market + product
copy "10_MARKET" "$REPO/VOICEOPS_MASTER_RESEARCH_FINDINGS_AND_ROADMAP_2026-08-18.md"
copy "11_MARKET" "$REPO/docs/VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md"
copy "12_MARKET" "$REPO/VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md"
copy "13_MARKET" "$REPO/VOICEOPS_SYSTEMS_ARCHITECTURE_BLUEPRINT_FOR_CLAUDE_CODE_2026-08-19.md"

# 3. Speed research
copy "20_SPEED" "$REPO/docs/openai-speed-research-2026-08-20.md"
copy "21_SPEED" "$REPO/docs/DEEP-RESEARCH-NETWORK-ARCHITECTURE-2026-08-20.md"

# 4. Humanness research
copy "30_HUMANNESS_BRIEF"           "$REPO/docs/HUMANNESS-RESEARCH-BRIEF-2026-08-20.md"
copy "31_HUMANNESS_RESPONSE1"       "$REPO/HUMANNESS-RECOMMENDATION-2026-08-20.md"
copy "32_HUMANNESS_RESPONSE2"       "$REPO/deep-research-report-humanness.md"
# Optional third humanness doc; add manually if present:
if [ -f "$REPO/deep-research-report-humanness-2.md" ]; then
    copy "33_HUMANNESS_RESPONSE3"   "$REPO/deep-research-report-humanness-2.md"
fi
if [ -f "$REPO/HUMANNESS-RECOMMENDATION-2.md" ]; then
    copy "33_HUMANNESS_RESPONSE3"   "$REPO/HUMANNESS-RECOMMENDATION-2.md"
fi

# 5. Historical audits
copy "40_HISTORICAL" "$REPO/docs/AUDIT_INTELLIGENCE_2026-08-04.md"
copy "41_HISTORICAL" "$REPO/docs/AUDIT_RESPONSE.md"
copy "42_HISTORICAL" "$REPO/docs/AUDIT_RESPONSE_2.md"
copy "43_HISTORICAL" "$REPO/docs/AUDIT_RESPONSE_3.md"
copy "44_HISTORICAL" "$REPO/docs/AUDIT_2026-08-05-runtime-failure-patterns.md"
copy "45_HISTORICAL" "$REPO/docs/AUDIT_VERIFICATION_2026-08-05.md"
copy "46_HISTORICAL" "$REPO/VOICEOPS_CODEBASE_AUDIT.md"
copy "47_HISTORICAL" "$REPO/VOICEOPS_REAUDIT_2026-08-02.md"

# 6. Bench + transcripts
copy "50_BENCH" "$REPO/docs/llm-ttft-bench-2026-08-20_012206.md"

# Include the transcripts folder
if [ -d "$REPO/docs/transcripts" ]; then
    mkdir -p "$STAGING/51_TRANSCRIPTS"
    cp "$REPO/docs/transcripts/"*.md "$STAGING/51_TRANSCRIPTS/" 2>/dev/null || true
    echo "  + 51_TRANSCRIPTS/ ($(ls -1 "$STAGING/51_TRANSCRIPTS" | wc -l | tr -d ' ') files)"
fi

# Zip everything
rm -f "$OUT"
(cd "$STAGING" && zip -r -q "$OUT" .)
echo
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Cleanup
rm -rf "$STAGING"

# List contents
echo
echo "bundle contents:"
unzip -l "$OUT" | tail -n +4 | head -50
