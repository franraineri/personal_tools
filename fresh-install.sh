#!/bin/bash
# fresh-install.sh
# Clears npm cache, removes node_modules & package-lock.json, reinstalls, and pushes the new lock file.
# Features: VPN check, interactive repo selector menu, quiet output with spinner.

# ─── Shared helpers (logging, VPN check, error handling). See utils.sh. ───────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/utils.sh" || { echo "Missing utils.sh in ${SCRIPT_DIR}" >&2; exit 1; }

# ─── Configuration ───────────────────────────────────────────────────────────
VPN_HOST="nexus3.disney.com"
LOG_DIR="$HOME/my_tools/logs"

REPOS=(
  "dcl-cruise-101-spa|$HOME/devTools/DCL/Silent/dcl-cruise-101-spa"
  "dcl-ui-global-components-library-v2|$HOME/devTools/DCL/Silent/dcl-ui-global-components-library-v2"
)

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# ─── Spinner Helper ──────────────────────────────────────────────────────────
spin() {
  local pid=$1
  local msg=$2
  local spinchars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
  local i=0
  local elapsed=0

  while kill -0 "$pid" 2>/dev/null; do
    local char="${spinchars:$((i % ${#spinchars})):1}"
    printf "\r  %s %s (%ds)" "$char" "$msg" "$elapsed"
    sleep 1
    ((elapsed++))
    ((i++))
  done
  printf "\r"
}

# ─── VPN Check (shared helper) ───────────────────────────────────────────────
echo ""
check_vpn "$VPN_HOST"

# ─── Repo Selection Menu ─────────────────────────────────────────────────────
echo ""
echo "Select repos to refresh (toggle number, enter to confirm):"
echo ""

SELECTED=()
for i in "${!REPOS[@]}"; do
  IFS='|' read -r name path <<< "${REPOS[$i]}"
  SELECTED+=("false")
  echo "  $((i+1))) [ ] $name"
done
echo "  a) Select all"
echo "  q) Quit"
echo ""

while true; do
  read -rp "Toggle option (1-${#REPOS[@]}, a=all, q=quit, enter=confirm): " choice

  if [[ "$choice" == "q" ]]; then
    echo "Aborted."
    exit 0
  elif [[ "$choice" == "a" ]]; then
    for i in "${!REPOS[@]}"; do
      SELECTED[$i]="true"
    done
  elif [[ "$choice" == "" ]]; then
    break
  elif [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#REPOS[@]} )); then
    idx=$((choice - 1))
    if [[ "${SELECTED[$idx]}" == "true" ]]; then
      SELECTED[$idx]="false"
    else
      SELECTED[$idx]="true"
    fi
  else
    echo "  Invalid option: $choice"
    continue
  fi

  # Redraw menu
  echo ""
  for i in "${!REPOS[@]}"; do
    IFS='|' read -r name path <<< "${REPOS[$i]}"
    if [[ "${SELECTED[$i]}" == "true" ]]; then
      echo "  $((i+1))) [x] $name"
    else
      echo "  $((i+1))) [ ] $name"
    fi
  done
  echo "  a) Select all"
  echo "  q) Quit"
  echo ""
done

# Check at least one selected
HAS_SELECTION=false
for sel in "${SELECTED[@]}"; do
  if [[ "$sel" == "true" ]]; then
    HAS_SELECTION=true
  fi
done

if [[ "$HAS_SELECTION" == "false" ]]; then
  echo "No repos selected. Exiting."
  exit 0
fi

# ─── Process Selected Repos ──────────────────────────────────────────────────
for i in "${!REPOS[@]}"; do
  if [[ "${SELECTED[$i]}" != "true" ]]; then
    continue
  fi

  IFS='|' read -r name path <<< "${REPOS[$i]}"

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  📂 $name"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  if [ ! -f "$path/package.json" ]; then
    echo "  ❌ No package.json found — skipping."
    continue
  fi

  cd "$path"
  LOG_FILE="$LOG_DIR/fresh-install-${name}.log"
  : > "$LOG_FILE"

  echo "  📋 Log: $LOG_FILE"
  echo ""

  # 1. Clear npm cache (non-critical)
  printf "  🧹 Clearing npm cache... "
  npm cache clean --force >> "$LOG_FILE" 2>&1 || true
  echo "done"

  # 2. Remove node_modules
  printf "  🗑️  Removing node_modules... "
  rm -rf node_modules
  echo "done"

  # 3. Remove package-lock.json
  printf "  🗑️  Removing package-lock.json... "
  rm -f package-lock.json
  echo "done"

  # 4. Reinstall (with spinner showing elapsed time)
  npm install >> "$LOG_FILE" 2>&1 &
  NPM_PID=$!
  spin $NPM_PID "📦 npm install..."

  wait $NPM_PID
  NPM_EXIT=$?

  if [[ $NPM_EXIT -ne 0 ]]; then
    echo "  ❌ npm install FAILED (exit $NPM_EXIT) — see log"
    continue
  fi
  echo "  ✅ npm install done"

  # 5. Verify package-lock.json was regenerated
  if [ ! -f "package-lock.json" ]; then
    echo "  ❌ package-lock.json was not regenerated — see log."
    continue
  fi

  # 6. Git: stage, commit and push
  printf "  📤 Committing & pushing... "
  BRANCH=$(git branch --show-current)

  if git add package-lock.json >> "$LOG_FILE" 2>&1 \
     && git commit -m "chore: regenerate package-lock.json" --no-verify >> "$LOG_FILE" 2>&1 \
     && git push origin "$BRANCH" --no-verify >> "$LOG_FILE" 2>&1; then
    echo "done → origin/$BRANCH"
  else
    echo "FAILED (see log)"
    continue
  fi

  echo "  ✅ $name refreshed successfully!"
done

echo ""
echo "🎉 All done!"
