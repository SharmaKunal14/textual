"""Reconcile a completed first-attempt GitHub observation pair, never a subset."""

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


def read(path, limit=64 * 1024 * 1024):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("artifact exceeds limit")
    return raw


def junit(raw):
    if b"\0" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("unsafe JUnit XML")
    raw.decode("utf-8")
    root = ET.fromstring(raw)
    if root.tag != "testsuites" or not len(root):
        raise ValueError("expected pytest JUnit suites")
    cases = {}
    for suite in root:
        if suite.tag != "testsuite":
            raise ValueError("unsupported JUnit suite")
        counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
        for case in suite.findall("testcase"):
            key = (case.attrib["classname"], case.attrib["name"])
            if key in cases or not all(key) or len(cases) >= 100_000:
                raise ValueError("duplicate, empty or excessive native case identities")
            results = [
                child for child in case if child.tag in {"error", "failure", "skipped"}
            ]
            if len(results) > 1:
                raise ValueError("ambiguous native case outcome")
            outcome = results[0].tag if results else "passed"
            skip = None
            if outcome == "skipped":
                skip = (
                    results[0].get("type", ""),
                    results[0].get("message", ""),
                    results[0].text or "",
                )
            cases[key] = (outcome, skip)
            counts["tests"] += 1
            if outcome != "passed":
                counts[
                    {"error": "errors", "failure": "failures", "skipped": "skipped"}[
                        outcome
                    ]
                ] += 1
        if any(int(suite.attrib[key]) != count for key, count in counts.items()):
            raise ValueError("JUnit counts disagree with case evidence")
    if not cases:
        raise ValueError("empty native test evidence")
    return cases


def seconds(start, end):
    start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("invalid GitHub timing boundaries")
    return (end - start).total_seconds()


def compare(root, observer_mode):
    if observer_mode not in {"observe", "observe-scoped"}:
        raise ValueError("only paired full observation experiments are supported")
    run = json.loads(read(root / "run.json"))
    jobs = json.loads(read(root / "jobs.json"))["jobs"]
    if (
        run["status"] != "completed"
        or run["event"] != "pull_request"
        or run["run_attempt"] != 1
        or not re.fullmatch(r"[a-f0-9]{40}", run["head_sha"])
        or len(jobs) != 2
        or {j["name"] for j in jobs} != {"native", observer_mode}
    ):
        raise ValueError("completed first-attempt paired PR run required")
    result = {"run": run["html_url"], "head": run["head_sha"], "attempt": 1, "jobs": {}}
    outcomes = {}
    executions = {}
    for job in jobs:
        mode = job["name"]
        folder = root / f"textual-{mode}-1"
        execution_raw = read(folder / "execution.json")
        execution = json.loads(execution_raw)
        if (
            job["status"] != "completed"
            or execution["head"] != run["head_sha"]
            or execution["mode"] != mode
            or execution["execution"] != "full"
            or execution["targets"] is not None
            or execution["planning_error"] is not None
            or type(execution["exit_code"]) is not int
            or execution["exit_code"] not in {0, 1}
            or job["conclusion"]
            != ("success" if execution["exit_code"] == 0 else "failure")
        ):
            raise ValueError("native execution identity or completion mismatch")
        raw = read(folder / "junit.xml")
        outcomes[mode] = junit(raw)
        executions[mode] = execution
        failed = sorted(
            "::".join(key)
            for key, value in outcomes[mode].items()
            if value[0] in {"error", "failure"}
        )
        if bool(failed) != bool(execution["exit_code"]):
            raise ValueError("native exit status contradicts JUnit")
        result["jobs"][mode] = {
            "url": job["html_url"],
            "id": job["id"],
            "conclusion": job["conclusion"],
            "cases": len(outcomes[mode]),
            "failed_cases": failed,
            "job_seconds": seconds(job["started_at"], job["completed_at"]),
            "trigger_to_result_seconds": seconds(
                run["created_at"], job["completed_at"]
            ),
            "queue_seconds": seconds(run["created_at"], job["started_at"]),
            "junit_sha256": hashlib.sha256(raw).hexdigest(),
            "execution_sha256": hashlib.sha256(execution_raw).hexdigest(),
            "execution": execution,
        }
    observation = json.loads(
        read(root / f"textual-{observer_mode}-1" / "observation.json")
    )
    unsigned = {key: value for key, value in observation.items() if key != "digest"}
    if (
        observation["digest"]
        != hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        or observation["commit"] != run["head_sha"]
        or observation["complete"] is not False
        or observation["omission_authorized"] is not False
        or observation["case_count"] != len(outcomes[observer_mode])
        or observation["exit_code"] != executions[observer_mode]["exit_code"]
        or type(observation["case_count"]) is not int
        or type(observation["exit_code"]) is not int
        or observation["schema"]
        != (
            "smartci.runtime-observation.v2"
            if observer_mode == "observe-scoped"
            else "smartci.runtime-observation.v1"
        )
        or observation.get("engine", "profile")
        != ("monitoring" if observer_mode == "observe-scoped" else "profile")
        or not re.fullmatch(r"[a-f0-9]{64}", observation["observer_sha256"])
        or executions["native"]["base"] != executions[observer_mode]["base"]
    ):
        raise ValueError("observation provenance or outcome mismatch")
    native, observed = outcomes["native"], outcomes[observer_mode]
    mismatches = sorted(
        "::".join(key)
        for key in native.keys() | observed.keys()
        if native.get(key) != observed.get(key)
    )
    result["comparison"] = {
        "case_identities_outcomes_and_skips_equal": not mismatches,
        "mismatched_cases": mismatches,
        "observer_extra_feedback_seconds": result["jobs"][observer_mode][
            "trigger_to_result_seconds"
        ]
        - result["jobs"]["native"]["trigger_to_result_seconds"],
        "total_runner_seconds": sum(
            job["job_seconds"] for job in result["jobs"].values()
        ),
        "actual_test_omissions": 0,
        "selective_ci_savings_demonstrated": False,
    }
    result["observation_digest"] = observation["digest"]
    result["observer_sha256"] = observation["observer_sha256"]
    result["limits"] = [
        "one paired run",
        "both paths execute the full suite",
        "observation cannot prove future dependency completeness",
    ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--mode", choices=["observe", "observe-scoped"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.artifacts, args.mode)
    with args.output.open("x") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
    return 0 if result["comparison"]["case_identities_outcomes_and_skips_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
