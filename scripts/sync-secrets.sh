#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
#  Sync shared secrets between GitHub Actions and Cloudflare Worker
#
#  Usage:
#    ./scripts/sync-secrets.sh                  # regenerate RUNNER_API_KEY + sync all
#    ./scripts/sync-secrets.sh --key-only       # only regenerate & sync RUNNER_API_KEY
#    ./scripts/sync-secrets.sh --check          # verify secrets are in sync (dry run)
# ─────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_SLUG=$(cd "$REPO_ROOT" && git remote get-url origin | sed 's|.*github.com[:/]||;s|\.git$||')

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "  ℹ $*"; }
success() { echo -e "  ${GREEN}✓${RESET} $*"; }
error()   { echo -e "  ${RED}✗${RESET} $*" >&2; }

MODE="${1:-sync}"

# ─────────────────────────────────────────────
# Check mode: verify both sides have the same secrets configured
# ─────────────────────────────────────────────
if [[ "$MODE" == "--check" ]]; then
  echo -e "${BOLD}Checking secret sync status...${RESET}\n"

  GH_SECRETS=$(gh secret list --repo "$REPO_SLUG" --json name -q '.[].name' | sort)
  WR_SECRETS=$(cd "$REPO_ROOT/worker" && npx wrangler secret list 2>/dev/null \
    | python3 -c "import json,sys; [print(s['name']) for s in json.load(sys.stdin)]" | sort)

  # Shared secrets that must exist on both sides
  SHARED=("RUNNER_API_KEY" "TELEGRAM_BOT_TOKEN" "CALLBACK_TOKEN")

  ALL_OK=true
  for s in "${SHARED[@]}"; do
    GH=$(echo "$GH_SECRETS" | grep -cx "$s")
    WR=$(echo "$WR_SECRETS" | grep -cx "$s")
    if [[ "$GH" -eq 1 && "$WR" -eq 1 ]]; then
      success "$s — both sides configured"
    elif [[ "$GH" -eq 1 && "$WR" -eq 0 ]]; then
      error "$s — missing on Cloudflare Worker"
      ALL_OK=false
    elif [[ "$GH" -eq 0 && "$WR" -eq 1 ]]; then
      error "$s — missing on GitHub Actions"
      ALL_OK=false
    else
      error "$s — missing on BOTH sides"
      ALL_OK=false
    fi
  done

  echo ""
  if $ALL_OK; then
    success "All shared secrets are configured on both sides."
    info "Note: this only checks existence, not value equality."
    info "Run ${BOLD}./scripts/sync-secrets.sh${RESET} to regenerate & sync values."
  else
    error "Some secrets are out of sync. Run ${BOLD}./scripts/sync-secrets.sh${RESET} to fix."
  fi
  exit 0
fi

# ─────────────────────────────────────────────
# Sync: regenerate and set shared secrets on both sides
# ─────────────────────────────────────────────
echo -e "${BOLD}Syncing shared secrets: GitHub Actions ↔ Cloudflare Worker${RESET}"
echo -e "  Repo: $REPO_SLUG\n"

sync_secret() {
  local name="$1"
  local value="$2"

  gh secret set "$name" --repo "$REPO_SLUG" --body "$value"
  echo "$value" | (cd "$REPO_ROOT/worker" && npx wrangler secret put "$name" 2>&1 | grep -v "WARNING\|update available")
  success "$name → GitHub + Cloudflare"
}

# ── RUNNER_API_KEY: always regenerate ──
NEW_KEY=$(openssl rand -hex 32)
echo -e "\n${YELLOW}Regenerating RUNNER_API_KEY...${RESET}"
sync_secret "RUNNER_API_KEY" "$NEW_KEY"

if [[ "$MODE" == "--key-only" ]]; then
  echo -e "\n${GREEN}${BOLD}Done.${RESET} Restart runners to pick up the new key:"
  echo -e "  gh run list --status in_progress -q '.[].databaseId' | xargs -I{} gh run cancel {}"
  echo -e "  gh workflow run runner-a.yml && gh workflow run runner-b.yml"
  exit 0
fi

# ── CALLBACK_TOKEN: regenerate ──
NEW_CB_TOKEN=$(openssl rand -hex 32)
echo -e "\n${YELLOW}Regenerating CALLBACK_TOKEN...${RESET}"
sync_secret "CALLBACK_TOKEN" "$NEW_CB_TOKEN"

echo -e "\n${GREEN}${BOLD}All shared secrets synced.${RESET}"
echo -e "\n${YELLOW}⚠ You must restart runners for changes to take effect:${RESET}"
echo -e "  gh run list --status in_progress -q '.[].databaseId' | xargs -I{} gh run cancel {}"
echo -e "  gh workflow run runner-a.yml && gh workflow run runner-b.yml"
