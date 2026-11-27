"""Native versus production Smart CI, followed by advisory coverage reporting.

Run on a reviewed public Textual PR without credentials. The native test command,
selection policy and full fallback come unchanged from benchmark.py.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import benchmark
from compare_observation import junit, read
from runtime_candidate import profile


def reconcile_inventory(nodeids, cases, targets):
    # Use the actual installed native pytest's JUnit naming, including parameters.
    from _pytest.junitxml import mangle_test_address

    if not isinstance(nodeids, list) or not nodeids or len(nodeids) > 100_000:
        raise ValueError("missing collected case identities")
    expected = set()
    for nodeid in nodeids:
        if not isinstance(nodeid, str):
            raise TypeError("invalid collected case")
        if targets is not None and nodeid.split("::")[0] not in targets:
            continue
        address = mangle_test_address(nodeid)
        identity = (".".join(address[:-1]), address[-1])
        if identity in expected:
            raise ValueError("ambiguous JUnit identity")
        expected.add(identity)
    return bool(expected) and expected == set(cases)


def receipt(output, execution, environment, run_id):
    raw = read(output / "coverage.xml", 16 * 1024 * 1024)
    cases = junit(read(output / "junit.xml"))
    expected = json.loads(read(output / "head-cases.json"))
    failed = any(value[0] in {"failure", "error"} for value in cases.values())
    reconciled = (
        execution["exit_code"] in {0, 1}
        and failed == bool(execution["exit_code"])
        and reconcile_inventory(expected, cases, execution["targets"])
    )
    return {
        "schema_version": "smartci.coverage-receipt.v1",
        "repository": "SharmaKunal14/textual",
        "revision": execution["head"],
        "environment": hashlib.sha256(
            json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "coverage_sha256": hashlib.sha256(raw).hexdigest(),
        "source_prefix": "src/textual",
        "run_url": f"https://github.com/SharmaKunal14/textual/actions/runs/{run_id}",
        "execution_scope": execution["execution"],
        "inventory_reconciled": reconciled,
        "test_count": len(cases),
        "exit_code": execution["exit_code"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["native", "smart"], required=True)
    parser.add_argument("--base", type=benchmark.revision, required=True)
    parser.add_argument("--head", type=benchmark.revision, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--planner-python", required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(Path.cwd().resolve()):
        parser.error("output must be outside application")
    output.mkdir(parents=True, exist_ok=True)
    current = (
        subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=10)
        .decode()
        .strip()
    )
    if current != args.head:
        raise ValueError("checkout differs from PR revision")
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], check=True, timeout=10)
    environment = profile()
    benchmark.write(output / "profile.json", environment)
    started = time.monotonic()
    code = benchmark.main(
        [
            "--mode",
            args.mode,
            "--base",
            args.base,
            "--head",
            args.head,
            "--output",
            str(output),
            "--planner-python",
            args.planner_python,
        ]
    )
    subprocess.run(["git", "diff", "--exit-code", "HEAD"], check=True, timeout=10)
    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=10)
        .decode()
        .strip()
        != args.head
    ):
        raise ValueError("revision changed during execution")
    if profile() != environment:
        raise ValueError("native environment changed during execution")
    execution = json.loads(read(output / "execution.json"))
    report_started = time.monotonic()
    report_code = None
    if args.mode == "smart":
        print(
            "Reporting changed-code coverage; selection is already fixed...", flush=True
        )
        receipt_path = output / "coverage-receipt.json"
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if (
            not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
            or os.environ.get("GITHUB_REPOSITORY") != "SharmaKunal14/textual"
        ):
            raise ValueError("expected real Textual GitHub run")
        try:
            proof = receipt(output, execution, environment, run_id)
            benchmark.write(receipt_path, proof)
        except (ValueError, OSError, KeyError, TypeError, ET.ParseError) as exc:
            # The CLI emits missing evidence rather than claiming measured zero.
            benchmark.write(
                output / "coverage-receipt-error.json",
                {"error_type": type(exc).__name__},
            )
            print(
                "Coverage receipt unavailable; report will identify missing evidence.",
                flush=True,
            )
            receipt_path = None
        command = [
            str(Path(args.planner_python).with_name("smartci")),
            "coverage-gaps",
            "--repo",
            str(Path.cwd()),
            "--repository",
            "SharmaKunal14/textual",
            "--base",
            args.base,
            "--head",
            args.head,
            "--environment",
            str(output / "profile.json"),
            "--output",
            str(output / "coverage-gaps.json"),
            "--audit",
            str(output / "coverage-gaps.md"),
        ]
        if receipt_path is not None:
            command.extend(
                [
                    "--receipt",
                    str(receipt_path),
                    "--coverage",
                    str(output / "coverage.xml"),
                ]
            )
        try:
            report_code = subprocess.run(command, check=False, timeout=60).returncode
        except (OSError, subprocess.TimeoutExpired):
            report_code = 2
    execution.update(
        reporting_seconds=time.monotonic() - report_started
        if args.mode == "smart"
        else 0.0,
        reporting_exit=report_code,
        measured_command_seconds=time.monotonic() - started,
        model_calls=0,
        harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    benchmark.write(output / "execution.json", execution)
    return code if code else (report_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
