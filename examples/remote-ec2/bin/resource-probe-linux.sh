#!/usr/bin/env bash
# resource-probe-linux.sh: a Linux memory-pressure probe for a resource gate.
#
# Prints one line and sets the exit code:
#   ok ...         exit 0   start more work
#   throttle ...   exit 1   wait
#   unknown ...    exit 2   could not read /proc/meminfo
#
# It reads /proc/pressure/memory (PSI, "some avg10") when the kernel has it,
# and /proc/meminfo (MemAvailable / MemTotal). A gate written for macOS
# (vm_stat, memory_pressure) finds neither tool here and may throttle forever;
# see ../resource-gate-linux.md.
#
#   PROC_ROOT                default /proc (tests point it at fake files)
#   PSI_SOME_AVG10_MAX       default 10   (percent of time stalled, last 10 s)
#   MEM_AVAILABLE_MIN_PCT    default 10
set -euo pipefail

proc="${PROC_ROOT:-/proc}"
psi_max="${PSI_SOME_AVG10_MAX:-10}"
mem_min="${MEM_AVAILABLE_MIN_PCT:-10}"

meminfo="${proc}/meminfo"
if [ ! -r "$meminfo" ]; then
  echo "unknown: cannot read ${meminfo}"
  exit 2
fi
total_kb="$(awk '/^MemTotal:/ {print $2}' "$meminfo")"
avail_kb="$(awk '/^MemAvailable:/ {print $2}' "$meminfo")"
if [ -z "$total_kb" ] || [ -z "$avail_kb" ] || [ "$total_kb" -le 0 ]; then
  echo "unknown: MemTotal or MemAvailable missing"
  exit 2
fi
avail_pct=$(( avail_kb * 100 / total_kb ))

psi_note="psi=unavailable"
psi_high=0
if [ -r "${proc}/pressure/memory" ]; then
  avg10="$(awk '$1 == "some" { for (i = 2; i <= NF; i++) if ($i ~ /^avg10=/) { sub(/^avg10=/, "", $i); print $i } }' "${proc}/pressure/memory")"
  if [ -n "$avg10" ]; then
    psi_note="psi_some_avg10=${avg10}"
    psi_high="$(awk -v a="$avg10" -v m="$psi_max" 'BEGIN { print (a + 0 > m + 0) ? 1 : 0 }')"
  fi
fi

if [ "$avail_pct" -lt "$mem_min" ]; then
  echo "throttle: mem_available=${avail_pct}% < ${mem_min}% ${psi_note}"
  exit 1
fi
if [ "$psi_high" -eq 1 ]; then
  echo "throttle: ${psi_note} > ${psi_max} mem_available=${avail_pct}%"
  exit 1
fi
echo "ok: mem_available=${avail_pct}% ${psi_note}"
