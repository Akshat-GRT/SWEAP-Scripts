#!/usr/bin/env python3
import json
import re
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Any
from pathlib import Path

JSON_PATH = r"path/to/json"
LOG_PATH  = r"path/to/log file"

CANON_STATUSES = {
    "PASSED": "PASSED",
    "FAILED": "FAILED",
    "SKIPPED": "SKIPPED",
    "XFAILED": "XFAILED",
    "XPASS": "XPASS",
    "ERROR": "ERROR",
    "OK": "PASSED",
    "IGNORED": "SKIPPED",
}

RUST_TEST_LINE_RE = re.compile(
    r"""^test\s+
        (?P<name>.+?)        # module path or doc-test path
        \s+\.\.\.\s+
        (?P<status>ok|FAILED|ignored)
        (?:\b.*)?$""",
    re.VERBOSE,
)

RUST_JSON_LINE_RE = re.compile(r'^\s*\{.*"type"\s*:\s*"test".*\}\s*$')

RUST_SUITE_JSON_KEYS = {"type", "event"}

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
    size = p.stat().st_size
    if size == 0:
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
        # Try NDJSON
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

def _parse_rust_json_lines(txt: str) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for i, raw in enumerate(txt.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if not (line.startswith("{") and ("\"type\"" in line) and ("\"event\"" in line)):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "test":
            continue
        name = str(obj.get("name", "")).strip()
        if not name:
            continue
        ev = str(obj.get("event", "")).strip().lower()
        if ev == "ok":
            results[name] = "PASSED"
        elif ev == "failed":
            results[name] = "FAILED"
        elif ev == "ignored":
            results[name] = "SKIPPED"
    return results

def _parse_rust_text_lines(txt: str) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for raw in txt.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        m = RUST_TEST_LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        status_raw = m.group("status")
        if status_raw == "ok":
            status = "PASSED"
        elif status_raw == "FAILED":
            status = "FAILED"
        elif status_raw == "ignored":
            status = "SKIPPED"
        else:
            status = _canon_status(status_raw)
        results[name] = status
    return results

def parse_rust_log(path_str: str) -> Dict[str, str]:
    p = Path(path_str).expanduser().resolve()
    print(f"[INFO] Reading log from: {p}")
    txt = _ensure_file_ok(p, "Log file")

    json_results = _parse_rust_json_lines(txt)
    if json_results:
        print(f"[INFO] Detected Rust NDJSON format; parsed {len(json_results)} tests from LOG")
        return json_results

    text_results = _parse_rust_text_lines(txt)
    if text_results:
        print(f"[INFO] Detected Rust text format; parsed {len(text_results)} tests from LOG")
        return text_results

    raise ValueError("Parsed log but found zero Rust test outcome lines. Check log format or flags.")

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

def main():
    try:
        json_results = parse_results_json(JSON_PATH)
        log_results = parse_rust_log(LOG_PATH)
        result = compare(json_results, log_results)
        print_report(result)
    except Exception as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
