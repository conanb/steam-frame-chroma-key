#!/bin/bash
# Generate compositor shader variants and switch the active key mode.
#   mrkey.sh chroma  [R G B threshold softness spill]  (virtual colour reveals camera)
#   mrkey.sh reverse [R G B threshold softness spill]  (camera colour reveals virtual scene)
#   mrkey.sh rect [x0 y0 x1 y1 [invert]]
#   mrkey.sh passthru <gain>|off [brightness]
#   mrkey.sh off
# activate/deactivate are used by mrtoggle.sh and retain the last generated mode.
set -euo pipefail
cd "$(dirname "$0")"
SHADER=distort_ps_achromatic_se2.spv
ORIG=/opt/steamvr/resources/shaders/vulkan/$SHADER
STATE="$HOME/.local/share/mrchroma"
CONF="$HOME/.config/mrchroma/mrchroma.conf"
OUT="$STATE/active_se2.spv"
mkdir -p "$STATE"

mode="${1:-}"; shift || true
active_mode() {
  if [[ -s "$STATE/active_mode" ]]; then cat "$STATE/active_mode"
  elif [[ -s "$STATE/mrkey.state" ]]; then awk '{print $1}' "$STATE/mrkey.state"
  else echo chroma; fi
}
is_active() {
  grep -q "^override = $SHADER " "$CONF" 2>/dev/null || grep -q '^reverse = 1$' "$CONF" 2>/dev/null
}
camera_name() { echo "tracked_camera_reprojection_simplified_${1}_nv12_ps.spv"; }
reload_shaders() {
  touch "$CONF"
  LD_LIBRARY_PATH=/opt/steamvr/bin/linuxarm64 timeout 10 /opt/steamvr/bin/linuxarm64/vrcmd --reload-shaders || true
}
camera_settings() {
  local gain=1 bright=1
  if [[ -s "$STATE/passthru.state" ]]; then
    read -r gain bright < "$STATE/passthru.state" || true
    if [[ "$gain" == off ]]; then gain=1; bright=1; fi
    bright="${bright:-1}"
  fi
  echo "$gain $bright"
}
brightness_on() {
  [[ -s "$STATE/passthru.state" ]] && [[ "$(head -n 1 "$STATE/passthru.state")" != off* ]]
}
generate_reverse_cameras() {
  local gain bright v cam output
  read -r gain bright < <(camera_settings)
  for v in sharpen_rgb rgb; do
    cam=$(camera_name "$v")
    output="$STATE/active_reverse_cam_$v.spv"
    spirv-dis "/opt/steamvr/resources/shaders/vulkan/$cam" -o "$STATE/orig_cam_$v.spvasm"
    python3 tools/gen_cam_spv.py "$STATE/orig_cam_$v.spvasm" "$output.new" "$gain" "$bright" reverse "$@"
  done
  for v in sharpen_rgb rgb; do
    output="$STATE/active_reverse_cam_$v.spv"
    mv "$output.new" "$output"
  done
}
set_overrides() {
  local selected="$1" v cam path
  if [[ "$selected" == on ]]; then
    if [[ "$(active_mode)" == reverse ]]; then
      for v in sharpen_rgb rgb; do
        path="$STATE/active_reverse_cam_$v.spv"
        [[ -s "$path" ]] || { echo "mrkey: missing $path; run mrkey.sh reverse ..." >&2; exit 1; }
      done
    else
      [[ -s "$OUT" ]] || { echo "mrkey: missing $OUT; run mrkey.sh chroma ..." >&2; exit 1; }
    fi
  fi
  sed -i "/^override = $SHADER /d; /^override = tracked_camera_reprojection_simplified_.*_nv12_ps.spv /d; /^reverse = /d" "$CONF"
  if [[ "$selected" == on ]]; then
    if [[ "$(active_mode)" == reverse ]]; then
      for v in sharpen_rgb rgb; do
        cam=$(camera_name "$v")
        path="$STATE/active_reverse_cam_$v.spv"
        echo "override = $cam $path" >> "$CONF"
      done
      echo 'reverse = 1' >> "$CONF"
    else
      echo "override = $SHADER $OUT" >> "$CONF"
    fi
  fi
  # Passthrough brightness remains available when reverse mode is off.
  if { [[ "$selected" != on ]] || [[ "$(active_mode)" != reverse ]]; } && brightness_on; then
    for v in sharpen_rgb rgb; do
      path="$STATE/active_cam_$v.spv"
      if [[ -s "$path" ]]; then
        cam=$(camera_name "$v")
        echo "override = $cam $path" >> "$CONF"
      fi
    done
  fi
  reload_shaders
}

case "$mode" in
  passthru)
    was_active=off
    is_active && was_active=on
    if [[ "${1:-off}" != off ]]; then
      for v in sharpen_rgb rgb; do
        cam=$(camera_name "$v")
        spirv-dis "/opt/steamvr/resources/shaders/vulkan/$cam" -o "$STATE/orig_cam_$v.spvasm"
        python3 tools/gen_cam_spv.py "$STATE/orig_cam_$v.spvasm" "$STATE/active_cam_$v.spv.new" "$1" "${2:-1}"
        mv "$STATE/active_cam_$v.spv.new" "$STATE/active_cam_$v.spv"
      done
      echo "$1 ${2:-1}" > "$STATE/passthru.state"
    else
      echo off > "$STATE/passthru.state"
      rm -f "$STATE/active_cam_sharpen_rgb.spv" "$STATE/active_cam_rgb.spv"
    fi
    if [[ "$(active_mode)" == reverse ]]; then
      read -r saved_mode r g b thr soft spill < "$STATE/mrkey.state"
      [[ "$saved_mode" == reverse ]] || { echo 'mrkey: reverse state missing' >&2; exit 1; }
      generate_reverse_cameras "$r" "$g" "$b" "$thr" "$soft" "$spill"
    fi
    set_overrides "$was_active"
    echo "mrkey: passthru ${1:-off} ${2:-}"
    ;;
  chroma|rect)
    if [[ "$mode" == chroma ]]; then
      [[ $# -ge 6 ]] || set -- 0 1 0 0.18 0.10 0.5
    else
      [[ $# -ge 4 ]] || set -- 0.3 0.3 0.7 0.7
    fi
    spirv-dis "$ORIG" -o "$STATE/orig_se2.spvasm"
    python3 tools/gen_key_spv.py "$STATE/orig_se2.spvasm" "$OUT.new" "$mode" "$@"
    mv "$OUT.new" "$OUT"
    echo "$mode $*" > "$STATE/mrkey.state"
    echo "$mode" > "$STATE/active_mode"
    set_overrides on
    echo "mrkey: $mode $*"
    ;;
  reverse)
    [[ $# -ge 6 ]] || set -- 0 1 0 0.18 0.10 0.5
    generate_reverse_cameras "$@"
    echo "reverse $*" > "$STATE/mrkey.state"
    echo reverse > "$STATE/active_mode"
    set_overrides on
    echo "mrkey: reverse $*"
    ;;
  activate)
    set_overrides on
    echo on
    ;;
  off|deactivate)
    set_overrides off
    echo off
    ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac
