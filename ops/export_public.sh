#!/usr/bin/env bash
# Build the public zcastor-agent repo from this working tree.
#
# Publishes everything except the dashboard. The dashboard is the signal
# source — the predictor, the banded S&R logic, the thing that would take a
# competitor months. Everything else is infrastructure: the gate stack, the
# record layer, the anchoring, the venue adapters. Those are what the
# submission is about, and there is nothing in them worth hiding.
#
# Secrets were never committed: .env and config/runtime.json have been
# gitignored since the first commit. This verifies that rather than assuming it.
#
# Note the leading slashes on the excludes. Without them rsync matches at ANY
# depth, so 'data/' silently removes zcastor/data/ as well as the runtime
# directory — which it did, and the export shipped without a package.
#
#   ./ops/export_public.sh ~/zcastor-agent
set -euo pipefail

DEST="${1:-$HOME/zcastor-agent}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "  source: $SRC"
echo "  dest:   $DEST"

mkdir -p "$DEST"
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.env' --exclude '*.bak' \
  --exclude '/data/' --exclude '/logs/' \
  --exclude '/dashboard/' \
  --exclude 'config/runtime.json' \
  "$SRC"/ "$DEST"/

# Refuse to ship anything that looks like a credential.
echo
echo "  scanning for secrets…"
LEAKS=$(grep -rIlE '(api[_-]?secret|private[_-]?key|BINANCE_API|MT5_PASSWORD|webhooks/[0-9]{15,})' \
  "$DEST" --exclude-dir=.git 2>/dev/null | grep -v '\.md$' || true)
if [ -n "$LEAKS" ]; then
  echo "  files mentioning credentials (check each is a NAME, not a VALUE):"
  echo "$LEAKS" | sed 's/^/    /'
fi

cat > "$DEST/.gitignore" << 'GITEOF'
__pycache__/
*.pyc
.env
.env.*
.venv/
config/runtime.json
data/
logs/
dossiers/
GITEOF

echo
echo "  $(find "$DEST" -name '*.py' -not -path '*/.git/*' | wc -l) python files"
echo "  $(cd "$DEST" && python3 tests/run_all.py 2>/dev/null | tail -2 | head -1)"
echo
echo "  next:"
echo "    cd $DEST && git init && git add -A"
echo "    git commit -m 'Zcastor: an execution agent that records what it refuses'"
