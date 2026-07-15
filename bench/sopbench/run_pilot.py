#!/usr/bin/env python3
"""Run SOPBench with or without SOPilot supervision (Arm A vs Arm B).

Usage (from the SOPBench repo root, inside its venv):
    python run_pilot.py --arm A --domain university --num_tasks 3 [sopbench args...]
    python run_pilot.py --arm B --domain university --num_tasks 3 [sopbench args...]

Everything after our two flags is passed straight to their run_simulation.py.
Arm output dirs are kept separate (output/armA, output/armB) so their
run_evaluation.py scores each arm independently with matching flags.
"""
from __future__ import annotations

import os
import sys

SOPBENCH_ROOT = os.environ.get("SOPBENCH_ROOT", os.getcwd())
sys.path.insert(0, SOPBENCH_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    argv = sys.argv[1:]
    if "--arm" not in argv:
        print("required: --arm A|B (A = baseline, B = SOPilot-supervised)")
        sys.exit(2)
    i = argv.index("--arm")
    arm = argv[i + 1].upper()
    rest = argv[:i] + argv[i + 2 :]

    os.chdir(SOPBENCH_ROOT)
    import run_simulation
    import swarm.core as swarm_core

    if arm == "B":
        import supervisor_hook

        supervisor_hook.patch(swarm_core, run_simulation)
        print("[pilot] SOPilot supervision hook ACTIVE (Arm B)")
    else:
        print("[pilot] baseline run (Arm A)")

    outdir = f"./output/arm{arm}"
    if "--output_dir" not in rest:
        rest += ["--output_dir", outdir]
    sys.argv = ["run_simulation.py"] + rest
    run_simulation.main()


if __name__ == "__main__":
    main()
