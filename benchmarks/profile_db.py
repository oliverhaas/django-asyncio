#!/usr/bin/env python
"""Profile the db single-row hot path with py-spy.

Launches granian under py-spy (so it can attach without ptrace_scope=0),
drives load with oha for most of the recording window, and writes a
flamegraph SVG plus a raw sample file. Run it once per config to compare
hotspots between sync and async.

  .venv/bin/python benchmarks/profile_db.py --config async
  .venv/bin/python benchmarks/profile_db.py --config sync10
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run import CONFIGS, _SEED_SCRIPT, build_granian_cmd, wait_for_health  # noqa: E402


def _seed():
    env = {**os.environ, "BENCH_DB": "postgres", "BENCH_PG_POOL": "1"}
    subprocess.run(
        [sys.executable, "-c", _SEED_SCRIPT],
        cwd=str(HERE), env=env, check=True,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", choices=list(CONFIGS), default="async")
    p.add_argument("--concurrency", type=int, default=100)
    p.add_argument("--duration", type=int, default=60,
                   help="py-spy total recording window (seconds). Generous "
                   "to drown out the ~5-10s of Django import samples.")
    p.add_argument("--oha-duration", type=int, default=55)
    p.add_argument("--rate", type=int, default=500)
    p.add_argument("--server-cpu", default="0")
    p.add_argument("--loadgen-cpus", default="1-8")
    args = p.parse_args()

    _seed()

    out_dir = HERE / "profiles"
    out_dir.mkdir(exist_ok=True)
    svg = out_dir / f"db_{args.config}.svg"
    raw = out_dir / f"db_{args.config}.raw"

    granian_cmd = build_granian_cmd(
        sys.executable, args.config, "127.0.0.1", 8123,
        server_cpus=args.server_cpu,
    )

    # Run py-spy twice: once for SVG flamegraph, once for raw text samples.
    # Each run is independent so the two files don't share noise.
    pyspy = str(Path(sys.executable).parent / "py-spy")
    for out, fmt in ((svg, "flamegraph"), (raw, "raw")):
        cmd = [pyspy, "record", "-o", str(out), "-f", fmt,
               "-d", str(args.duration), "-s", "--gil",
               "--rate", str(args.rate),
               "--", *granian_cmd]
        env = {**os.environ, "BENCH_DB": "postgres", "BENCH_PG_POOL": "1"}
        print(f"\n[profile] {fmt}: {' '.join(cmd)}", flush=True)
        server = subprocess.Popen(cmd, cwd=str(HERE), env=env,
                                  start_new_session=True)
        try:
            wait_for_health("http://127.0.0.1:8123")
            cfg = CONFIGS[args.config]
            url = f"http://127.0.0.1:8123/db/{cfg['path']}/"
            oha = [
                "taskset", "-c", args.loadgen_cpus,
                os.path.expanduser("~/.cargo/bin/oha"),
                "-z", f"{args.oha_duration}s",
                "-c", str(args.concurrency),
                "--no-tui", "--output-format", "quiet",
                url,
            ]
            print(f"[profile] driving load: oha c={args.concurrency} "
                  f"for {args.oha_duration}s", flush=True)
            subprocess.run(oha, capture_output=True, check=False)
            server.wait(timeout=60)
            print(f"[profile] wrote {out}", flush=True)
        finally:
            # Always sweep the process group, even when py-spy exited cleanly.
            # py-spy doesn't forward SIGTERM to its child's worker, so the
            # granian worker is left orphaned holding stdout open and any
            # downstream `tee`/`tail` blocks forever.
            import signal
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(server.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    server.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue


if __name__ == "__main__":
    main()
