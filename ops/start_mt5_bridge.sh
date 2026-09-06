#!/usr/bin/env bash
# Starts the MT5 terminal and the rpyc bridge inside a Wine prefix.
#
# mt5linux does not talk to the terminal directly — it connects over rpyc to a
# Windows Python running INSIDE the same prefix, which imports the MetaTrader5
# package. Terminal running but bridge not started is the most common reason
# "MT5 is logged in" and "the engine cannot connect" are both true at once.
#
# Prereqs, all inside the prefix:
#   Windows Python 3.10, then in that python:  pip install MetaTrader5 rpyc
set -euo pipefail

export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
export WINEDEBUG="${WINEDEBUG:--all}"

PORT="${MT5_BRIDGE_PORT:-18812}"
TERMINAL="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
WINE_PYTHON="${WINE_PYTHON:-$WINEPREFIX/drive_c/Program Files/Python310/python.exe}"

for f in "$TERMINAL" "$WINE_PYTHON"; do
  [[ -f "$f" ]] || { echo "missing: $f"; echo "prefix in use: $WINEPREFIX"; exit 1; }
done

if ! pgrep -f "terminal64.exe" > /dev/null; then
  echo "[mt5-bridge] starting terminal..."
  wine "$TERMINAL" &
  sleep 8
else
  echo "[mt5-bridge] terminal already running"
fi

echo "[mt5-bridge] starting rpyc bridge on $PORT (prefix: $WINEPREFIX)"
exec wine "$WINE_PYTHON" -c "
import sys
from rpyc.utils.server import ThreadedServer
from rpyc.core.service import SlaveService
import MetaTrader5
print('[mt5-bridge] listening on $PORT'); sys.stdout.flush()
ThreadedServer(SlaveService, port=$PORT,
               protocol_config={'allow_all_attrs': True}).start()
"
