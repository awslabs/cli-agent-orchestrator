# A resource gate that works on Linux

Some kits only start new agents when the machine has room. They check memory
pressure first and wait while it's high. That's a *resource gate*.

## The trap

A gate written on a Mac usually calls `vm_stat` or `memory_pressure`. Neither
exists on Linux. Depending on how the gate handles errors, a missing command
means one of two things:

- **fail closed:** it reads as "no room", so the gate throttles forever and
  nothing ever starts. You see no error, only an idle box;
- **fail open:** it reads as "plenty of room", so the gate never throttles.

Either way, the gate isn't measuring anything. Check which way yours fails
before you trust it on the box.

## What to read on Linux instead

| Signal | File | Meaning |
|---|---|---|
| Memory pressure (PSI) | `/proc/pressure/memory`, the `some avg10=` value | Share of the last 10 seconds in which at least one task waited for memory. Ubuntu 24.04 kernels have PSI. |
| Available memory | `/proc/meminfo`, `MemAvailable` ÷ `MemTotal` | Memory the kernel can hand out without swapping. Better than `MemFree`. |

[`bin/resource-probe-linux.sh`](bin/resource-probe-linux.sh) reads both. It
prints `ok ...` (exit 0), `throttle ...` (exit 1) or `unknown ...` (exit 2),
so a gate can call it and branch on the exit code:

```bash
if /usr/local/lib/cao-box/resource-probe-linux.sh; then
  echo "room to start another agent"
fi
```

The thresholds are `PSI_SOME_AVG10_MAX` (default 10) and
`MEM_AVAILABLE_MIN_PCT` (default 10). If the kernel has no PSI, the probe
uses memory alone and says `psi=unavailable`.

## Making an existing gate Linux-aware

1. Branch on `uname -s`. Keep the macOS probe for the laptop and call the
   Linux probe on the box.
2. Treat `unknown` as an error you can see. Log it and alert. Don't turn it
   quietly into "throttle" or "ok".
3. Give any loop that waits for room a limit, for example "give up after 30
   minutes and say so", so a broken probe can't stall the box forever.
4. Disk matters too. Gate on free space for the data volume (`df -P <home>`)
   if your agents build or clone a lot.
