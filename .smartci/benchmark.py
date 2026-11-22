"""Bounded public-repository experiment; native failures remain job failures."""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def revision(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", value):
        raise argparse.ArgumentTypeError("full commit SHA required")
    return value


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def checkout(root: Path, sha: str) -> None:
    subprocess.run(
        ["git", "checkout", "--detach", sha], cwd=root, check=True, timeout=30
    )


def create_plan(
    root: Path, base: str, head: str, output: Path, target_python: str
) -> None:
    from smartci.discovery.collector import collect
    from smartci.discovery.models import PythonConfig
    from smartci.discovery.planning import plan
    from smartci.selection import SelectionPolicy

    config = PythonConfig(
        schema_version="1",
        repository="SharmaKunal14/textual",
        source_roots=["src"],
        test_roots=["tests"],
        granularity="file",
    )
    policy = SelectionPolicy(
        schema_version="1",
        repository=config.repository,
        mode="selective",
        must_run=[],
        unknown_evidence="full",
        allow_source_excerpts=False,
    )
    snapshots = []
    try:
        for label, commit in (("base", base), ("head", head)):
            print(f"Collecting {label} dependency map at {commit}...", flush=True)
            checkout(root, commit)
            snapshot, _, _ = collect(root, config, target_python)
            snapshots.append(snapshot)
            write(
                output / f"{label}-compatibility.json",
                {
                    "commit": commit,
                    "complete": snapshot.graph.complete,
                    "files": len(snapshot.inventory.tests),
                    "cases": snapshot.provenance.native_case_count,
                    "plugins": snapshot.provenance.plugins,
                    "gaps": [gap.model_dump() for gap in snapshot.provenance.gaps],
                    "snapshot_digest": snapshot.digest,
                },
            )
        result = plan(root, snapshots[0], snapshots[1], policy)
        repeated = plan(root, snapshots[0], snapshots[1], policy)
        if result != repeated:
            raise ValueError("planning was not deterministic")
        write(output / "selection.json", result.model_dump())
        # A full decision always executes the independent native command.
        targets = (
            sorted(snapshots[1].provenance.native_targets[t] for t in result.selected)
            if result.execution == "subset"
            else None
        )
        write(output / "targets.json", targets)
    finally:
        checkout(root, head)


def selected_targets(path: Path) -> list[str] | None:
    value = json.loads(path.read_text())
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 10000
        or any(
            not isinstance(item, str)
            or not re.fullmatch(r"tests/[A-Za-z0-9_./-]+\.py", item)
            or ".." in item.split("/")
            for item in value
        )
    ):
        raise ValueError("invalid selected file targets")
    return sorted(set(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["native", "smart", "plan", "observe", "observe-scoped"],
        required=True,
    )
    parser.add_argument("--base", type=revision, required=True)
    parser.add_argument("--head", type=revision, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--planner-python", default=sys.executable)
    parser.add_argument("--target-python", default=sys.executable)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    output = args.output.resolve()
    if output.is_relative_to(root):
        parser.error("output must be outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    if args.mode == "plan":
        create_plan(root, args.base, args.head, output, args.target_python)
        return 0

    started = time.monotonic()
    planning_seconds = 0.0
    targets = None
    planning_error = None
    if args.mode == "smart":
        plan_started = time.monotonic()
        try:
            # Separate process bounds discovery and permits restoring HEAD on failure.
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
                    "--target-python",
                    sys.executable,
                ],
                check=True,
                timeout=180,
            )
            targets = selected_targets(output / "targets.json")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            planning_error = type(exc).__name__
            print(
                f"Planning unavailable ({planning_error}); running full suite.",
                flush=True,
            )
        finally:
            checkout(root, args.head)
        planning_seconds = time.monotonic() - plan_started
    print(f"Executing {'subset' if targets else 'full native suite'}...", flush=True)
    test_started = time.monotonic()
    command = [
        sys.executable,
        "-m",
        "pytest",
        *(targets or ["tests"]),
        "-v",
        "--cov=./src/textual",
        f"--cov-report=xml:{output / 'coverage.xml'}",
        "--cov-report=term-missing",
        f"--junitxml={output / 'junit.xml'}",
    ]
    observing = args.mode in {"observe", "observe-scoped"}
    if observing:
        command.extend(
            [
                "-p",
                "smartci.adapters.runtime_observer",
                "--smartci-observation",
                str(output / "observation.json"),
            ]
        )
        if args.mode == "observe-scoped":
            command.extend(
                [
                    "--smartci-observation-engine",
                    "monitoring",
                    "--smartci-observation-contexts",
                ]
            )
    try:
        code = subprocess.run(
            command, check=False, timeout=900 if observing else 720
        ).returncode
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
        },
    )
    return code if code >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
