"""Shadow-only runtime candidate experiment. The native full job remains required."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from benchmark import native_command, revision, selected_targets, write
from compare_observation import compare, read

REPOSITORY = "SharmaKunal14/textual"


def profile():
    packages = sorted(
        (re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(), d.version)
        for d in importlib.metadata.distributions()
        if d.metadata["Name"].lower() != "smartci"
    )
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "packages": [list(p) for p in packages],
        "profile": "pytest-tests-verbose-native-coverage-v1",
        "instrumentation_exclusion": "smartci only; installed without dependencies",
    }


def enrollment(folder, base, current_profile):
    run = json.loads(read(folder / "run.json"))
    if (
        run["repository"]["full_name"] != REPOSITORY
        or run["head_repository"]["full_name"] != REPOSITORY
        or run["path"] != ".github/workflows/smartci-pilot.yml"
        or run["head_branch"] != "codex/runtime-candidate-enrollment"
        or run["head_sha"] != base
        or run["conclusion"] != "success"
    ):
        raise ValueError("expected passing enrollment PR provenance")
    comparison = compare(folder, "observe-scoped")
    if not comparison["comparison"]["case_identities_outcomes_and_skips_equal"]:
        raise ValueError("observer changed native case outcomes")
    for mode in ("native", "observe-scoped"):
        if comparison["jobs"][mode]["execution"]["exit_code"] != 0:
            raise ValueError("passing enrollment required")
        if (
            json.loads(read(folder / f"textual-{mode}-1" / "profile.json"))
            != current_profile
        ):
            raise ValueError("native environment profile mismatch")
    return comparison


def plan(root, base, head, folder, output):
    from smartci.investigation.runtime_scope import candidate

    comparison = enrollment(folder, base, json.loads(read(output / "profile.json")))
    raw = read(folder / "textual-observe-scoped-1" / "observation.json")
    report = candidate(root, base, head, raw)
    if report != candidate(root, base, head, raw):
        raise ValueError("candidate generation not deterministic")
    write(output / "candidate.json", report)
    write(output / "enrollment.json", comparison)
    # A broad/uncertain result uses the independent native command, not an inventory expansion.
    targets = report["candidate_files"]
    write(
        output / "targets.json",
        None if targets == report["inventory_files"] else targets,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["native", "observe-scoped", "candidate", "plan"],
        required=True,
    )
    parser.add_argument("--base", type=revision, required=True)
    parser.add_argument("--head", type=revision, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enrollment", type=Path)
    parser.add_argument("--planner-python")
    args = parser.parse_args()
    root, output = Path.cwd().resolve(), args.output.resolve()
    if output.is_relative_to(root):
        parser.error("output must be outside source checkout")
    output.mkdir(parents=True, exist_ok=True)
    if args.mode == "plan":
        if not args.enrollment:
            parser.error("enrollment is required")
        plan(root, args.base, args.head, args.enrollment, output)
        return 0
    current = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=10)
        .decode()
        .strip()
    )
    if current != args.head:
        raise ValueError("checkout differs from PR head")
    subprocess.run(["git", "diff", "--quiet", "HEAD"], check=True, timeout=10)
    write(output / "profile.json", profile())
    started = time.monotonic()
    planning_seconds, planning_error, targets = 0.0, None, None
    if args.mode == "candidate":
        print(
            "Validating enrollment and computing experimental candidate...", flush=True
        )
        plan_started = time.monotonic()
        try:
            if not args.enrollment or not args.planner_python:
                raise ValueError("enrollment and isolated planner required")
            subprocess.run(
                [
                    args.planner_python,
                    str(Path(__file__).resolve()),
                    "--mode",
                    "plan",
                    "--base",
                    args.base,
                    "--head",
                    args.head,
                    "--output",
                    str(output),
                    "--enrollment",
                    str(args.enrollment.resolve()),
                ],
                check=True,
                timeout=60,
            )
            targets = selected_targets(output / "targets.json")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            planning_error = type(exc).__name__
            print(
                f"Candidate unavailable ({planning_error}); executing full suite.",
                flush=True,
            )
        planning_seconds = time.monotonic() - plan_started
    print(
        f"Executing {len(targets) if targets else 'all'} native test files; independent full control required.",
        flush=True,
    )
    command = native_command(sys.executable, output, targets)
    if args.mode == "observe-scoped":
        command.extend(
            [
                "-p",
                "smartci.adapters.runtime_observer",
                "--smartci-observation",
                str(output / "observation.json"),
                "--smartci-observation-engine",
                "monitoring",
                "--smartci-observation-contexts",
            ]
        )
    test_started = time.monotonic()
    try:
        code = subprocess.run(command, check=False, timeout=900).returncode
    except subprocess.TimeoutExpired:
        code = 124
    write(
        output / "execution.json",
        {
            "mode": args.mode,
            "base": args.base,
            "head": args.head,
            "execution": "subset" if targets else "full",
            "targets": targets,
            "planning_error": planning_error,
            "planning_seconds": planning_seconds,
            "test_seconds": time.monotonic() - test_started,
            "command_seconds": time.monotonic() - started,
            "exit_code": code,
            "required_execution": "full",
            "omission_authorized": False,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )
    return code if code >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
