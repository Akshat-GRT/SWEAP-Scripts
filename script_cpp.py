#!/usr/bin/env python3
import json
import re
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Any
from pathlib import Path

JSON_PATH = r"path/to/json"
LOG_PATH  = r"path/to/gtest.log"

# Canonical statuses
CANON_STATUSES = {
    "PASSED": "PASSED",
    "FAILED": "FAILED",
    "SKIPPED": "SKIPPED",
    "XFAILED": "XFAILED",
    "XPASS": "XPASS",
    "ERROR": "ERROR",
    "OK": "PASSED",
}

GTEST_OUTCOME_RE = re.compile(
    r"""^\[\s*
        (?P<tag>OK|FAILED|SKIPPED)
        \s*\]\s+
        (?P<name>.+?)
        (?:\s+\(.*\))?
        \s*$""",
    re.VERBOSE,
)

GTEST_SUMMARY_FAIL_RE = re.compile(
    r"""^\[\s*FAILED\s*\]\s+(?P<name>.+?)\s*$"""
)

@dataclass
class ComparisonResult:
    same: bool
    counts_equal: bool
    names_equal: bool
    statuses_equal: bool
    count_json: int
    count_log: int
    only_in_json: List[str]
    only_in_log: List[str]
    status_mismatches: List[Tuple[str, str, str]]

def _canon_status(s: str) -> str:
    s = (s or "").strip().upper()
    return CANON_STATUSES.get(s, s)

def _ensure_file_ok(p: Path, label: str) -> str:
    if not p.exists():
        raise FileNotFoundError(f"{label} not found: {p}")
    if p.is_dir():
        raise IsADirectoryError(f"{label} is a directory, not a file: {p}")
    if p.stat().st_size == 0:
        raise ValueError(f"{label} is empty: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ValueError(f"{label} contains only whitespace: {p}")
    return text

def _extract_tests_array(obj: Any) -> List[dict]:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("tests", "items", "results", "cases"):
            if isinstance(obj.get(k), list):
                return obj[k]
    raise ValueError("JSON does not contain a list of tests (list of objects with 'name' and 'status').")

def parse_results_json(path_str: str) -> Dict[str, str]:
    p = Path(path_str).expanduser().resolve()
    print(f"[INFO] Reading JSON from: {p}")
    txt = _ensure_file_ok(p, "JSON file")

    try:
        data = json.loads(txt)
        tests = _extract_tests_array(data)
    except json.JSONDecodeError:
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        objs = []
        for i, ln in enumerate(lines, 1):
            try:
                objs.append(json.loads(ln))
            except json.JSONDecodeError:
                preview = (ln[:160] + "…") if len(ln) > 160 else ln
                raise ValueError(
                    f"JSON parse failed. Not valid JSON or NDJSON.\n"
                    f"Problem at line {i}: {preview}\n"
                    f"First 160 chars of file:\n{txt[:160]!r}"
                ) from None
        tests = _extract_tests_array(objs)

    result: Dict[str, str] = {}
    for i, item in enumerate(tests):
        if not isinstance(item, dict):
            raise ValueError(f"Test entry {i} is not an object: {item!r}")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"Test entry {i} missing 'name'")
        status = _canon_status(str(item.get("status", "")).strip())
        result[name] = status
    if not result:
        raise ValueError("Parsed JSON but found zero tests.")
    print(f"[INFO] Parsed {len(result)} tests from JSON")
    return result

def _parse_gtest_json(txt: str) -> Dict[str, str]:
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError:
        return {}

    if not isinstance(obj, dict) or "testsuites" not in obj:
        return {}

    results: Dict[str, str] = {}
    for ts in obj.get("testsuites", []):
        cases = ts.get("testsuite") or ts.get("tests", []) or []
        for tc in cases:
            suite = (tc.get("classname") or ts.get("name") or "").strip()
            test = str(tc.get("name", "")).strip()
            if not test:
                continue
            name = f"{suite}.{test}" if suite else test

            if str(tc.get("status", "")).upper() == "SKIPPED" or str(tc.get("result", "")).upper() == "SKIPPED":
                results[name] = "SKIPPED"
                continue

            ok = tc.get("ok", None)
            if isinstance(ok, bool):
                results[name] = "PASSED" if ok else "FAILED"
                continue

            res = str(tc.get("result", "")).strip().upper()
            if res in ("PASSED", "SUCCESS", "COMPLETED"):
                results[name] = "PASSED"
            elif res in ("FAILED", "FAILURE"):
                results[name] = "FAILED"
            elif res == "SKIPPED":
                results[name] = "SKIPPED"

    return results

def _sanitize_gtest_name(name: str) -> str:
    name = re.sub(r"\s+\(.*\)\s*$", "", name).strip()
    return name

def parse_gtest_log(path_str: str) -> Dict[str, str]:
    p = Path(path_str).expanduser().resolve()
    print(f"[INFO] Reading log from: {p}")
    txt = _ensure_file_ok(p, "Log file")

    json_results = _parse_gtest_json(txt)
    if json_results:
        print(f"[INFO] Detected gtest JSON; parsed {len(json_results)} tests from LOG")
        return json_results

    results: Dict[str, str] = {}
    for raw in txt.splitlines():
        line = raw.rstrip()
        if not line:
            continue

        m = GTEST_OUTCOME_RE.match(line)
        if m:
            tag = m.group("tag").upper()
            name = _sanitize_gtest_name(m.group("name"))
            status = _canon_status(tag)
            results[name] = status
            continue

        m2 = GTEST_SUMMARY_FAIL_RE.match(line)
        if m2:
            name = _sanitize_gtest_name(m2.group("name"))
            results[name] = "FAILED"
            continue

    if not results:
        raise ValueError("Parsed log but found zero gtest outcome lines. Check log format.")
    print(f"[INFO] Parsed {len(results)} tests from LOG")
    return results

def compare(json_results: Dict[str, str], log_results: Dict[str, str]) -> ComparisonResult:
    json_names = set(json_results)
    log_names = set(log_results)

    only_in_json = sorted(json_names - log_names)
    only_in_log = sorted(log_names - json_names)

    status_mismatches = [
        (n, json_results[n], log_results[n])
        for n in sorted(json_names & log_names)
        if json_results[n] != log_results[n]
    ]

    counts_equal = len(json_names) == len(log_names)
    names_equal = not only_in_json and not only_in_log
    statuses_equal = not status_mismatches
    same = counts_equal and names_equal and statuses_equal

    return ComparisonResult(
        same=same,
        counts_equal=counts_equal,
        names_equal=names_equal,
        statuses_equal=statuses_equal,
        count_json=len(json_names),
        count_log=len(log_names),
        only_in_json=only_in_json,
        only_in_log=only_in_log,
        status_mismatches=status_mismatches,
    )

def print_report(result: ComparisonResult) -> None:
    banner = "MATCH" if result.same else "DIFFERENCES FOUND"
    print("=" * 72)
    print(banner)
    print("=" * 72)
    print(f"Counts equal:   {result.counts_equal} (JSON={result.count_json}, LOG={result.count_log})")
    print(f"Names equal:    {result.names_equal}")
    print(f"Statuses equal: {result.statuses_equal}")
    print("-" * 72)

    if result.only_in_json:
        print("Present only in JSON:")
        for n in result.only_in_json:
            print(f"  • {n}")
        print("-" * 72)

    if result.only_in_log:
        print("Present only in LOG:")
        for n in result.only_in_log:
            print(f"  • {n}")
        print("-" * 72)

    if result.status_mismatches:
        print("Status mismatches:")
        for n, js, ls in result.status_mismatches:
            print(f"  • {n}\n      JSON={js} | LOG={ls}")
        print("-" * 72)

    if result.same:
        print("All checks passed — counts, names, and statuses match.")
    else:
        print("See details above for differences.")

    print("\nJSON_DIFF =")
    print(json.dumps(asdict(result), indent=2))

    print("=" * 72)
    final_status = "PASS" if result.same else "FAIL"
    print(f"FINAL REVIEW STATUS: {final_status}")
    print("=" * 72)

def main():
    try:
        json_results = parse_results_json(JSON_PATH)
        log_results = parse_gtest_log(LOG_PATH)
        result = compare(json_results, log_results)
        print_report(result)

    except Exception as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()

    """
    Example Gtest Log:
    ```
    $ ./my_tests
[==========] Running 4 tests from 2 test suites.
[----------] Global test environment set-up.
[----------] 2 tests from MathTests
[ RUN      ] MathTests.Addition
[       OK ] MathTests.Addition (0 ms)
[ RUN      ] MathTests.Subtraction
[       OK ] MathTests.Subtraction (1 ms)
[----------] 2 tests from MathTests (1 ms total)
```
    """
