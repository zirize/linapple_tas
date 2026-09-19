#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Watches what the emulator costs the host.

Four numbers matter and they are not the same.

* **CPU share** says how much work is being done.
* **Wakeups per second** says how often a core is pulled out of idle.
* **Core frequency** is the thing that is actually complained about: the clock
  goes to the top, the package heats, the fan spins up. Share and wakeups are
  only ways of predicting it, and predictions have been wrong here before — so
  the frequency is now read rather than inferred.
* **Tctl** is where that ends up, and it is the number a person hears.

A process can sit at one percent and still hold a machine at its top frequency
by waking it five hundred times a second. It can also sit at forty percent on
one core and be quieter than the same work sprayed across eight, because each
core it touches is boosted out of its deepest idle state — so how many cores a
process migrates over is reported too.

    ./tools/cpumon.py                  # one measurement
    ./tools/cpumon.py --watch 5        # every five seconds
    ./tools/cpumon.py --watch 5 --log ~/.cache/apple2player-cpu.csv

The monitor samples frequency four times a second while it measures, which is
the rate its own rule 4 in PROTOCOL.md allows a polling tool; it is idle in
between.

Thresholds are advisory: they flag the shapes that have actually caused trouble
here rather than any absolute limit.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CLOCK_TICKS = os.sysconf("SC_CLK_TCK")

# Both were measured on this project: an idle session once sat at 500 wakeups a
# second, and an automation loop at 768, with CPU share reading near zero.
IDLE_WAKEUP_WARN = 150
BUSY_WAKEUP_WARN = 300

# Measured on this machine (Ryzen 9 5900X, amd-pstate-epp, powersave governor):
# nothing running at all still floats around 3.8 GHz because the desktop and
# other VMs keep something awake, so a flat threshold would fire constantly.
# What matters is a core held high by *this* process while it does little.
IDLE_FREQ_WARN_MHZ = 3600
FREQ_SAMPLE_HZ = 4
CPUFREQ = "/sys/devices/system/cpu/cpu{}/cpufreq/{}"
CPUIDLE = Path("/sys/devices/system/cpu")

# The deepest state the idle governor offers. Time spent here is the thing the
# whole CPU budget exists to protect: a core that never reaches it keeps its
# clock and its heat up no matter how little work it does.
DEEP_IDLE_WARN_PERCENT = 90

# Where this machine's fan becomes audible. Tccd is the chiplet silicon, Tctl
# the control figure the fan curve follows; either one alone is enough, because
# a single pinned thread heats one chiplet without moving the package much.
TCCD_WARN_C = 75.0
TCTL_WARN_C = 80.0


@dataclass
class Sample:
    cpu_seconds: float
    wakeups: int
    timestamp: float


def core_frequency(cpu: int) -> float:
    """MHz for one core. `cpuinfo_avg_freq` is what the hardware actually ran
    at over the last interval; `scaling_cur_freq` is only what was asked for,
    so it is the fallback rather than the first choice."""
    for name in ("cpuinfo_avg_freq", "scaling_cur_freq"):
        try:
            with open(CPUFREQ.format(cpu, name), encoding="utf-8") as handle:
                return int(handle.read()) / 1000.0
        except (OSError, ValueError):
            continue
    return 0.0


def running_core(pid: int) -> int | None:
    """Which core the process was last scheduled on."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return int(handle.read().rsplit(") ", 1)[1].split()[36])
    except (OSError, IndexError, ValueError):
        return None


def idle_counters() -> tuple[int, int]:
    """(microseconds spent in the deepest idle state, times any state was left).

    Read across every core, without root: `/proc` context switches are a proxy
    for this, and a per-process one at that. These are the counters the idle
    governor itself keeps, so "the cores are not going to sleep" stops being an
    inference drawn from wakeup counts and becomes something that was measured.
    """
    deep_us = exits = 0
    for cpu in sorted(CPUIDLE.glob("cpu[0-9]*/cpuidle")):
        states = sorted(cpu.glob("state*"))
        if not states:
            continue
        for state in states:
            try:
                exits += int((state / "usage").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        try:
            deep_us += int((states[-1] / "time").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return deep_us, exits


def temperatures() -> dict[str, float]:
    """Every labelled CPU temperature this machine exposes, in Celsius.

    On Ryzen that is Tctl — the control figure the fan curve follows, which is
    offset above the real junction temperature on some parts — and one Tccd per
    chiplet, which is the actual silicon. Reading both matters: one hot chiplet
    is what a single pinned thread looks like, and it can spin the fan up while
    Tctl still reads calm.

    Root is not needed for any of it, and `conky_temp` on this machine reads the
    same three, so the numbers here and on the desktop panel agree.
    """
    found: dict[str, float] = {}
    for path in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if path.joinpath("name").read_text(encoding="utf-8").strip() not in (
                    "k10temp", "coretemp", "zenpower"):
                continue
            for label in sorted(path.glob("temp*_label")):
                name = label.read_text(encoding="utf-8").strip()
                value = label.with_name(label.name.replace("_label", "_input"))
                found[name] = int(value.read_text(encoding="utf-8")) / 1000.0
        except (OSError, ValueError):
            continue
    return found


def package_temperature() -> float | None:
    readings = temperatures()
    for name in ("Tctl", "Package id 0"):
        if name in readings:
            return readings[name]
    return None


def too_hot(readings: dict[str, float]) -> str | None:
    """Whether the machine is in the state the person actually complains about.

    Thresholds are the ones the fan curve on this box reacts at: a chiplet at
    75°C or the control figure at 80°C is where it becomes audible. They are
    deliberately about heat rather than about clocks — the clock is a step on
    the way, the noise is the symptom.
    """
    hot = [f"{name} {value:.1f}°C" for name, value in sorted(readings.items())
           if name.startswith("Tccd") and value >= TCCD_WARN_C]
    package = readings.get("Tctl", readings.get("Package id 0"))
    if package is not None and package > TCTL_WARN_C:
        hot.append(f"Tctl {package:.1f}°C")
    return " · ".join(hot) if hot else None


def find_emulators(pattern: str) -> list[tuple[int, str]]:
    result = subprocess.run(["pgrep", "-af", pattern],
                            capture_output=True, text=True)
    found = []
    for line in result.stdout.splitlines():
        pid_text, _, cmdline = line.partition(" ")
        if not pid_text.isdigit():
            continue
        # pgrep -f matches this tool's own command line too when the pattern is
        # spelled out in it; skipping self keeps the report honest.
        if int(pid_text) == os.getpid():
            continue
        found.append((int(pid_text), cmdline))
    return found


def sample(pid: int) -> Sample | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            fields = handle.read().split()
        cpu = (int(fields[13]) + int(fields[14])) / CLOCK_TICKS
        wakeups = 0
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(("voluntary_ctxt_switches",
                                    "nonvoluntary_ctxt_switches")):
                    wakeups += int(line.split(":")[1])
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
        return None
    return Sample(cpu, wakeups, time.monotonic())


def verdict(cpu_percent: float, wakes_per_second: float,
            mean_mhz: float) -> str:
    if wakes_per_second >= BUSY_WAKEUP_WARN:
        return "too many wakeups - raise the polling interval or the chunk size"
    if cpu_percent < 5 and wakes_per_second >= IDLE_WAKEUP_WARN:
        return "waking often with nothing to do - check that the idle loop blocks in the kernel"
    if cpu_percent < 5 and mean_mhz >= IDLE_FREQ_WARN_MHZ:
        # The shape this project keeps producing: almost no work done, and the
        # core still never comes down. Share alone would call this healthy.
        return "no work but a high clock - suspect short, frequent wakeups"
    if cpu_percent >= 85:
        return "nearly a whole core - lower wait_duty / pace_duty"
    return "normal"


@dataclass
class Window:
    cpu_percent: float
    wakes: float
    mean_mhz: float
    peak_mhz: float
    cores: int


def measure(pid: int, window: float) -> Window | None:
    """One measurement window, sampling the frequency as it goes.

    Frequency has to be sampled rather than read once at the end: the whole
    problem is a clock that spikes and falls back, and a single reading lands
    wherever it lands.
    """
    before = sample(pid)
    if before is None:
        return None
    freqs: list[float] = []
    cores: set[int] = set()
    deadline = time.monotonic() + window
    interval = 1.0 / FREQ_SAMPLE_HZ
    while time.monotonic() < deadline:
        core = running_core(pid)
        if core is not None:
            cores.add(core)
            freqs.append(core_frequency(core))
        time.sleep(min(interval, max(deadline - time.monotonic(), 0)))
    after = sample(pid)
    if after is None:
        return None
    elapsed = after.timestamp - before.timestamp
    return Window(
        cpu_percent=100 * (after.cpu_seconds - before.cpu_seconds) / elapsed,
        wakes=(after.wakeups - before.wakeups) / elapsed,
        mean_mhz=sum(freqs) / len(freqs) if freqs else 0.0,
        peak_mhz=max(freqs) if freqs else 0.0,
        cores=len(cores),
    )


def report_system(window: float) -> tuple[float, float]:
    """The machine as a whole: are the cores reaching their deepest idle state.

    This is the number the budget is really about, and it is not per-process:
    one busy neighbour keeps the package awake for everyone. Printing it beside
    the emulator's own figures is what stops "the emulator looks fine" from
    being read as "the machine is quiet".
    """
    deep_before, exits_before = idle_counters()
    start = time.monotonic()
    time.sleep(window)
    deep_after, exits_after = idle_counters()
    elapsed = time.monotonic() - start
    cores = os.cpu_count() or 1
    deep_percent = 100 * (deep_after - deep_before) / (elapsed * 1e6 * cores)
    exits = (exits_after - exits_before) / elapsed
    readings = temperatures()
    heat = "   " + "  ".join(f"{name} {value:4.1f}°C"
                             for name, value in sorted(readings.items())) \
        if readings else ""
    hot = too_hot(readings)
    if hot:
        note = f"   [HOT] {hot}"
    elif deep_percent < DEEP_IDLE_WARN_PERCENT:
        note = "   <- the core never reaches a deep idle state"
    else:
        note = ""
    print(f"[{time.strftime('%H:%M:%S')}] whole system       "
          f"deep idle {deep_percent:5.1f}%   "
          f"idle exits {exits:7.0f}/s{heat}{note}", flush=True)
    return deep_percent, exits


def report(pids: list[tuple[int, str]], window: float,
           log: Path | None = None) -> None:
    deep_percent, exits = report_system(window)
    temperature = package_temperature()
    for pid, cmdline in pids:
        result = measure(pid, window)
        if result is None:
            print(f"pid {pid}: gone")
            continue
        short = cmdline.split()[0].rsplit("/", 1)[-1]
        stamp = time.strftime("%H:%M:%S")
        heat = f"   Tctl {temperature:4.1f}°C" if temperature is not None else ""
        print(f"[{stamp}] pid {pid:<7} {short:<10} "
              f"CPU {result.cpu_percent:5.1f}%   wakeups {result.wakes:6.0f}/s   "
              f"clock {result.mean_mhz:5.0f}/{result.peak_mhz:.0f} MHz   "
              f"cores {result.cores}{heat}   "
              f"{verdict(result.cpu_percent, result.wakes, result.mean_mhz)}",
              flush=True)
        if log is not None:
            readings = temperatures()
            tccd = max((v for k, v in readings.items() if k.startswith("Tccd")),
                       default=None)
            line = (f"{time.time():.0f},{pid},{short},{result.cpu_percent:.1f},"
                    f"{result.wakes:.0f},{result.mean_mhz:.0f},"
                    f"{result.peak_mhz:.0f},{result.cores},"
                    f"{'' if temperature is None else f'{temperature:.1f}'},"
                    f"{'' if tccd is None else f'{tccd:.1f}'},"
                    f"{deep_percent:.1f},{exits:.0f}\n")
            with log.open("a", encoding="utf-8") as handle:
                handle.write(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="linapple --agent-socket",
                        help="process command line to match")
    parser.add_argument("--window", type=float, default=2.0,
                        help="seconds to average over (default: %(default)s)")
    parser.add_argument("--watch", type=float, default=0,
                        help="repeat every N seconds instead of measuring once")
    parser.add_argument("--log", type=Path,
                        help="append every measurement to this CSV as well, so "
                             "a long session can be looked at afterwards")
    args = parser.parse_args()

    if args.log and not args.log.exists():
        args.log.parent.mkdir(parents=True, exist_ok=True)
        args.log.write_text("epoch,pid,name,cpu_percent,wakeups_per_s,"
                            "mean_mhz,peak_mhz,cores,tctl_c,tccd_max_c,"
                            "deep_idle_percent,idle_exits_per_s\n",
                            encoding="utf-8")

    while True:
        pids = find_emulators(args.pattern)
        if not pids:
            print(f"no process matches '{args.pattern}'", flush=True)
            if not args.watch:
                return 1
        else:
            report(pids, args.window, args.log)
        if not args.watch:
            return 0
        time.sleep(max(args.watch - args.window, 0))


if __name__ == "__main__":
    raise SystemExit(main())
