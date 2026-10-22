#!/bin/bash
# mrtoggle — switch chroma keying on/off without regenerating the shader.
#   mrtoggle.sh on | off | toggle | status
# "on" re-enables the last shader mode and settings built by mrkey.sh.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SHADER=distort_ps_achromatic_se2.spv
CONF="$HOME/.config/mrchroma/mrchroma.conf"
is_on() { grep -q "^override = $SHADER " "$CONF" 2>/dev/null || grep -q '^reverse = 1$' "$CONF" 2>/dev/null; }

# PC home background (applied on the PC by pc/mrchroma_pcsync.pyw):
#   mrtoggle.sh bg on | off | toggle | status
#   on  = PC SteamVR background painted in the key colour (keys to passthrough)
#   off = PC SteamVR default background (mountains, floor)
PCBG="$HOME/.config/mrchroma/pcbg"
if [[ "${1:-}" == bg ]]; then
  cur=$(cat "$PCBG" 2>/dev/null || echo off)
  case "${2:-status}" in
    status) echo "$cur"; exit 0 ;;
    on|off) new=$2 ;;
    toggle) if [[ "$cur" == on ]]; then new=off; else new=on; fi ;;
    *) sed -n '15,18p' "$0"; exit 2 ;;
  esac
  echo "$new" > "$PCBG.tmp" && mv "$PCBG.tmp" "$PCBG"
  echo "bg $new"
  exit 0
fi

cmd="${1:-status}"
if [[ "$cmd" == toggle ]]; then
  if is_on; then cmd=off; else cmd=on; fi
fi
case "$cmd" in
  status)
    if is_on; then echo on; else echo off; fi
    exit 0 ;;
  on)
    if ! is_on; then "$DIR/mrkey.sh" activate >/dev/null; fi ;;
  off)
    if is_on; then "$DIR/mrkey.sh" deactivate >/dev/null; fi ;;
  *) sed -n '2,5p' "$0"; exit 2 ;;
esac
echo "$cmd"
