#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
#  byok-tg-runner setup script
#  Guides you through the full installation.
# ─────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}ℹ ${RESET}$*"; }
success() { echo -e "${GREEN}✓ ${RESET}$*"; }
warn()    { echo -e "${YELLOW}⚠ ${RESET}$*"; }
error()   { echo -e "${RED}✗ ${RESET}$*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}══ $* ${RESET}"; }
prompt()  { echo -e "${YELLOW}▶ ${RESET}$*"; }

pause() {
  echo ""
  read -rp "$(echo -e "${YELLOW}按 Enter 繼續...${RESET}")"
}

ask() {
  local var="$1"
  local msg="$2"
  local secret="${3:-false}"
  echo ""
  prompt "$msg"
  if [[ "$secret" == "true" ]]; then
    read -rs value
    echo ""
  else
    read -r value
  fi
  [[ -z "$value" ]] && error "不能為空"
  eval "$var=\"\$value\""
}

# ─────────────────────────────────────────────
# 0. 歡迎
# ─────────────────────────────────────────────
clear
echo -e "${BOLD}"
cat << 'EOF'
   _                _         _
  | |__  _   _  ___ | | __    | |_  __ _       _ __ _   _ _ __  _ __   ___ _ __
  | '_ \| | | |/ _ \| |/ /____| __|/ _` |_____| '__| | | | '_ \| '_ \ / _ \ '__|
  | |_) | |_| | (_) |   <_____| |_| (_| |_____| |  | |_| | | | | | | |  __/ |
  |_.__/ \__, |\___/|_|\_\     \__|\__, |     |_|   \__,_|_| |_|_| |_|\___|_|
         |___/                     |___/
EOF
echo -e "${RESET}"
echo -e "  Azure AI Foundry + Copilot SDK BYOK Telegram Runner"
echo -e "  Cloudflare Worker + GitHub Actions + Telegram Bot\n"
echo -e "  此腳本將引導你完成所有設定，約需 ${BOLD}10 分鐘${RESET}。\n"
pause

# ─────────────────────────────────────────────
# 1. 檢查前置工具
# ─────────────────────────────────────────────
step "步驟 1／9：檢查前置工具"

check_tool() {
  if command -v "$1" &>/dev/null; then
    success "$1 已安裝"
  else
    error "$1 未安裝，請先安裝後再執行此腳本。\n  安裝說明：$2"
  fi
}

check_tool gh       "https://cli.github.com"
check_tool node     "https://nodejs.org"
check_tool npx      "隨 Node.js 一起安裝"
check_tool python3  "https://www.python.org"
check_tool curl     "系統套件管理器"
check_tool openssl  "系統套件管理器"

# wrangler 可透過 npx 使用，不需全域安裝
if npx wrangler --version &>/dev/null 2>&1; then
  success "wrangler 可用（via npx）"
else
  error "wrangler 無法執行，請在 worker/ 目錄執行 npm install 後再試"
fi

# ─────────────────────────────────────────────
# 2. 確認工作目錄
# ─────────────────────────────────────────────
step "步驟 2／9：確認專案目錄"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
info "專案根目錄：$REPO_ROOT"

[[ -f "$REPO_ROOT/server/main.py" ]]          || error "找不到 server/main.py，請在 byok-tg-runner repo 根目錄的 scripts/ 執行此腳本"
[[ -f "$REPO_ROOT/worker/package.json" ]]     || error "找不到 worker/package.json"
[[ -f "$REPO_ROOT/.github/workflows/runner-a.yml" ]] || error "找不到 .github/workflows/runner-a.yml"

cd "$REPO_ROOT"

# 安裝 worker 依賴
if [[ ! -d "$REPO_ROOT/worker/node_modules" ]]; then
  info "安裝 worker npm 依賴..."
  (cd worker && npm install --silent)
fi
success "worker 依賴已就緒"

# ─────────────────────────────────────────────
# 3. GitHub 登入 & 建立 repo
# ─────────────────────────────────────────────
step "步驟 3／9：GitHub 設定"

if ! gh auth status &>/dev/null 2>&1; then
  info "尚未登入 GitHub CLI，即將啟動登入流程..."
  gh auth login
fi
GH_USER=$(gh api user --jq '.login')
success "已登入 GitHub：$GH_USER"

# 偵測是否已有 remote
REMOTE_URL=$(git remote get-url origin 2>/dev/null || true)
if [[ -n "$REMOTE_URL" ]]; then
  REPO_SLUG=$(echo "$REMOTE_URL" | sed 's|.*github.com[:/]||;s|\.git$||')
  success "已連接 GitHub repo：$REPO_SLUG"
else
  echo ""
  prompt "尚未連接 GitHub repo。"
  echo    "  輸入 repo 名稱（直接 Enter 使用預設 'byok-tg-runner'）："
  read -r REPO_NAME
  REPO_NAME="${REPO_NAME:-byok-tg-runner}"
  info "建立公開 repo：$GH_USER/$REPO_NAME ..."
  gh repo create "$REPO_NAME" --public --source=. --remote=origin --push
  REPO_SLUG="$GH_USER/$REPO_NAME"
  success "Repo 已建立：https://github.com/$REPO_SLUG"
fi

# ─────────────────────────────────────────────
# 4. Cloudflare 登入 & KV namespace
# ─────────────────────────────────────────────
step "步驟 4／9：Cloudflare 設定"

if ! (cd worker && npx wrangler whoami 2>&1 | grep -q "logged in\|OAuth Token"); then
  info "尚未登入 Cloudflare，即將啟動登入流程..."
  (cd worker && npx wrangler login)
fi

CF_ACCOUNT_ID=$(cd worker && npx wrangler whoami 2>&1 | grep -oE '[0-9a-f]{32}' | head -1)
success "Cloudflare Account ID：$CF_ACCOUNT_ID"

# 建立或複用 KV namespace
info "檢查 KV namespace RUNNER_KV..."
WORKER_NAME=$(grep '^name' worker/wrangler.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
EXISTING_KV_ID=$(cd worker && npx wrangler kv namespace list 2>&1 | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    for ns in data:
        if ns.get('title','').endswith('-RUNNER_KV'):
            print(ns['id']); break
except: pass
" 2>/dev/null)

if [[ -n "$EXISTING_KV_ID" ]]; then
  KV_NAMESPACE_ID="$EXISTING_KV_ID"
  warn "KV namespace 已存在，ID：$KV_NAMESPACE_ID"
else
  info "建立 KV namespace..."
  KV_OUTPUT=$(cd worker && npx wrangler kv namespace create "RUNNER_KV" 2>&1 || true)
  KV_NAMESPACE_ID=$(echo "$KV_OUTPUT" | grep -oE '[0-9a-f]{32}' | tail -1)
  success "KV namespace 已建立，ID：$KV_NAMESPACE_ID"
fi

# 更新 wrangler.toml 中的 KV ID
if grep -q 'id = "' worker/wrangler.toml; then
  sed -i "s|id = \"[^\"]*\"|id = \"$KV_NAMESPACE_ID\"|" worker/wrangler.toml
else
  # 若還沒有 kv_namespaces block，加入
  cat >> worker/wrangler.toml << EOF

[[kv_namespaces]]
binding = "RUNNER_KV"
id = "$KV_NAMESPACE_ID"
EOF
fi
success "wrangler.toml 已更新"

# ─────────────────────────────────────────────
# 5. 取得各項 Secret
# ─────────────────────────────────────────────
step "步驟 5／9：收集必要的 Secrets"

# ── Telegram Bot Token ──
echo ""
echo -e "${BOLD}[Telegram Bot Token]${RESET}"
info "請先在 Telegram 找 @BotFather，傳送 /newbot 指令建立 bot。"
info "建立完成後，BotFather 會給你一串 token，格式：123456789:AAF..."
ask TELEGRAM_BOT_TOKEN "貼上你的 Telegram Bot Token：" true

# 驗證 token 格式
if [[ ! "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]+:AA[A-Za-z0-9_-]{33}$ ]]; then
  warn "Token 格式看起來不太對，但仍繼續..."
fi

# ── Telegram Chat ID ──
echo ""
echo -e "${BOLD}[Telegram Chat ID]${RESET}"
info "在 Telegram 搜尋 @userinfobot，傳任意訊息給它。"
info "它會回覆你的 Chat ID（純數字）。"
ask TELEGRAM_CHAT_ID "貼上你的 Chat ID："

# ── Allowed Chat ID (for Wrangler) ──
echo ""
echo -e "${BOLD}[Allowed Chat ID]${RESET}"
info "此為 Worker 端允許接收訊息的 Chat ID（通常與上方相同）。"
echo ""
prompt "直接 Enter 使用與 Telegram Chat ID 相同的值（$TELEGRAM_CHAT_ID），或輸入不同值："
read -r ALLOWED_CHAT_ID_INPUT
ALLOWED_CHAT_ID="${ALLOWED_CHAT_ID_INPUT:-$TELEGRAM_CHAT_ID}"
success "ALLOWED_CHAT_ID = $ALLOWED_CHAT_ID"

# ── Cloudflare API Token ──
echo ""
echo -e "${BOLD}[Cloudflare API Token]${RESET}"
info "前往：https://dash.cloudflare.com/profile/api-tokens"
info "點選 'Create Token' → 選擇範本 'Edit Cloudflare Workers'。"
info "確認 Account Resources 選到你的帳號，然後建立 token。"
ask CF_API_TOKEN "貼上你的 Cloudflare API Token：" true

# ── RUNNER_API_KEY ──
echo ""
echo -e "${BOLD}[Runner API Key]${RESET}"
RUNNER_API_KEY=$(openssl rand -hex 32)
success "已自動生成 RUNNER_API_KEY（請勿對外分享）"
info "RUNNER_API_KEY = $RUNNER_API_KEY"

# ── GitHub PAT ──
echo ""
echo -e "${BOLD}[GitHub Personal Access Token]${RESET}"
info "前往：https://github.com/settings/tokens"
info "點選 'Generate new token (classic)'。"
info "勾選 'workflow' scope，然後生成 token。"
ask GH_PAT "貼上你的 GitHub PAT：" true

# ── Foundry API Key ──
echo ""
echo -e "${BOLD}[Azure AI Foundry API Key]${RESET}"
info "This is your Azure AI Foundry API key for BYOK mode."
info "前往 Azure AI Foundry portal 取得你的 API key。"
ask FOUNDRY_API_KEY "貼上你的 Foundry API Key：" true

# ─────────────────────────────────────────────
# 6. 設定 GitHub Secrets
# ─────────────────────────────────────────────
step "步驟 6／9：設定 GitHub Secrets"

set_secret() {
  gh secret set "$1" --repo "$REPO_SLUG" --body "$2"
  success "GitHub Secret 已設定：$1"
}

set_secret "TELEGRAM_BOT_TOKEN" "$TELEGRAM_BOT_TOKEN"
set_secret "TELEGRAM_CHAT_ID"   "$TELEGRAM_CHAT_ID"
set_secret "CF_ACCOUNT_ID"      "$CF_ACCOUNT_ID"
set_secret "CF_API_TOKEN"       "$CF_API_TOKEN"
set_secret "KV_NAMESPACE_ID"    "$KV_NAMESPACE_ID"
set_secret "RUNNER_API_KEY"     "$RUNNER_API_KEY"
set_secret "GH_PAT"            "$GH_PAT"
set_secret "FOUNDRY_API_KEY"   "$FOUNDRY_API_KEY"

# ─────────────────────────────────────────────
# 7. 設定 Wrangler Secrets & Deploy Worker
# ─────────────────────────────────────────────
step "步驟 7／9：Deploy Cloudflare Worker"

cd worker

echo "$RUNNER_API_KEY"     | npx wrangler secret put RUNNER_API_KEY     2>&1 | grep -v "WARNING\|update available"
echo "$TELEGRAM_BOT_TOKEN" | npx wrangler secret put TELEGRAM_BOT_TOKEN 2>&1 | grep -v "WARNING\|update available"
echo "$ALLOWED_CHAT_ID"    | npx wrangler secret put ALLOWED_CHAT_ID    2>&1 | grep -v "WARNING\|update available"
success "Wrangler secrets 已設定（RUNNER_API_KEY, TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_ID）"

info "部署 Worker..."
DEPLOY_OUTPUT=$(npx wrangler deploy 2>&1)
echo "$DEPLOY_OUTPUT" | grep -v "WARNING\|update available"

WORKER_URL=$(echo "$DEPLOY_OUTPUT" | grep -oE 'https://[a-z0-9-]+\.[a-z0-9]+\.workers\.dev' | head -1)
if [[ -z "$WORKER_URL" ]]; then
  warn "無法自動偵測 Worker URL，請手動查看上方輸出"
  ask WORKER_URL "請手動輸入 Worker URL（例：https://byok-tg-runner-worker.xxx.workers.dev）："
fi
success "Worker 已部署：$WORKER_URL"

cd ..

# ─────────────────────────────────────────────
# 8. 設定 Telegram Webhook
# ─────────────────────────────────────────────
step "步驟 8／9：設定 Telegram Webhook"

# 生成隨機 secret path
SECRET_PATH=$(openssl rand -hex 16)
WEBHOOK_URL="${WORKER_URL}/${SECRET_PATH}"

info "設定 webhook：$WEBHOOK_URL"
WEBHOOK_RESP=$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${WEBHOOK_URL}")
if echo "$WEBHOOK_RESP" | grep -q '"ok":true'; then
  success "Telegram webhook 已設定"
else
  warn "Webhook 設定回應：$WEBHOOK_RESP"
fi

# ─────────────────────────────────────────────
# 9. Push & 觸發第一次 Runner
# ─────────────────────────────────────────────
step "步驟 9／9：啟動 Runner"

# Push 最新程式碼（包含更新的 wrangler.toml）
git add worker/wrangler.toml 2>/dev/null || true
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -m "chore: update wrangler.toml KV namespace ID [setup]" 2>/dev/null || true
  git push 2>/dev/null || true
fi

info "觸發第一次 workflow run..."
gh workflow run runner-a.yml --repo "$REPO_SLUG"
success "Workflow 已觸發！"

# ─────────────────────────────────────────────
# 完成！
# ─────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  設定完成！${RESET}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${RESET}"
echo ""
echo -e "  ${BOLD}GitHub Repo${RESET}   https://github.com/$REPO_SLUG"
echo -e "  ${BOLD}Worker URL${RESET}    $WORKER_URL"
echo -e "  ${BOLD}Telegram Bot${RESET}  https://t.me/$(curl -sf "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | grep -oP '(?<="username":")[^"]+')"
echo ""
echo -e "  ${CYAN}驗證步驟：${RESET}"
echo -e "  1. 前往 https://github.com/$REPO_SLUG/actions 確認 runner 正在執行"
echo -e "  2. 傳訊息給你的 Telegram bot，等待 Azure AI Foundry 回覆"
echo ""
echo -e "  ${YELLOW}Runner 啟動需約 30 秒（安裝依賴 + tunnel 就緒）${RESET}"
echo ""
