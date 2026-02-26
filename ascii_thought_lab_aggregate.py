import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[WARN] failed to read: {path} ({e})", file=sys.stderr)
        return None
    try:
        data = json.loads(raw)
    except Exception as e:
        print(f"[WARN] invalid JSON: {path} ({e})", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"[WARN] JSON root is not an object: {path}", file=sys.stderr)
        return None
    return data


def _parse_stamp_from_filename(path: Path) -> str:
    """
    Expected filename: <provider>_<problem_id>_<YYYYMMDD>_<HHMMSS>.json
    """
    stem = path.stem
    parts = stem.split("_")
    if len(parts) >= 4 and (len(parts[-2]) == 8 and parts[-2].isdigit()) and (len(parts[-1]) == 6 and parts[-1].isdigit()):
        return f"{parts[-2]}_{parts[-1]}"
    return ""


def _join_list(val: Any) -> str:
    if not isinstance(val, list):
        return ""
    out: List[str] = []
    for x in val:
        if x is None:
            continue
        out.append(str(x))
    return ",".join(out)


def _get(d: Dict[str, Any], key: str, default: Any = "") -> Any:
    v = d.get(key, default)
    return default if v is None else v


def _get_nested(d: Dict[str, Any], keys: Tuple[str, ...], default: Any = "") -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _extract_row(path: Path, data: Dict[str, Any], *, include_text: bool) -> Dict[str, Any]:
    tests = _get(data, "tests", None)
    tests_d = tests if isinstance(tests, dict) else {}

    contrib = _get_nested(tests_d, ("contrib",), default={})
    contrib_d = contrib if isinstance(contrib, dict) else {}

    dtests = _get_nested(tests_d, ("diagram_tests",), default={})
    dtests_d = dtests if isinstance(dtests, dict) else {}

    row: Dict[str, Any] = {
        "file": str(path),
        "stamp": _parse_stamp_from_filename(path),
        "provider": _get(data, "provider", ""),
        "model": _get(data, "model", ""),
        "problem_id": _get(data, "problem_id", ""),
        "run_seed": _get(data, "run_seed", ""),
        "diagram_hash": _get(data, "diagram_hash", ""),
        "tags_n": len(_get(data, "tags", [])) if isinstance(_get(data, "tags", []), list) else "",
        "tags": _join_list(_get(data, "tags", [])),
        "unknown_tags_n": len(_get(data, "unknown_tags", [])) if isinstance(_get(data, "unknown_tags", []), list) else "",
        "phase_a_attempts": _get(data, "phase_a_attempts", ""),
        "phase_a_validation_errors_n": len(_get(data, "phase_a_validation_errors", []))
        if isinstance(_get(data, "phase_a_validation_errors", []), list) else "",
        "test_mode": _get(tests_d, "test_mode", ""),
        "no_tags_similarity": _get(contrib_d, "no_tags_similarity", ""),
        "no_diagram_similarity": _get(contrib_d, "no_diagram_similarity", ""),
        "neither_similarity": _get(contrib_d, "neither_similarity", ""),
        "corrupt_similarity": _get(dtests_d, "corrupt_similarity", ""),
        "swap_used": _get(dtests_d, "swap_used", ""),
        "swap_similarity": _get(dtests_d, "swap_similarity", ""),
        "tamper_remove_used": _get(tests_d, "tamper_remove_used", ""),
        "tamper_add_used": _get(tests_d, "tamper_add_used", ""),
        "tamper_remove_similarity": _get(tests_d, "tamper_remove_similarity", ""),
        "tamper_add_similarity": _get(tests_d, "tamper_add_similarity", ""),
        "tamper_both_similarity": _get(tests_d, "tamper_both_similarity", ""),
    }

    if include_text:
        row["caption_1line"] = _get(data, "caption_1line", "")
        row["answer"] = _get(data, "answer", "")

    # Optional: future metrics (safe to keep even if missing)
    field = _get(data, "field_metrics", None)
    field_d = field if isinstance(field, dict) else {}
    row["field_scope"] = _get(field_d, "scope", "")
    row["field_time_layer"] = _get(field_d, "time_layer", "")
    row["field_time_every"] = _get(field_d, "time_every", "")

    return row


def _iter_json_files(root: Path, *, recursive: bool, pattern: str) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    if not root.exists():
        return
    if recursive:
        yield from root.rglob(pattern)
    else:
        yield from root.glob(pattern)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dir", type=str, default="runs", help="Directory (or a single JSON file)")
    ap.add_argument("--out", type=str, default="-", help="Output CSV path (default: stdout)")
    ap.add_argument("--pattern", type=str, default="*.json", help="Glob pattern (default: *.json)")
    ap.add_argument("--recursive", action="store_true", help="Recurse into subdirectories")
    ap.add_argument("--include-text", action="store_true", help="Include caption_1line and full answer in CSV")

    args = ap.parse_args()

    root = Path(args.in_dir)
    files = sorted(_iter_json_files(root, recursive=bool(args.recursive), pattern=str(args.pattern)))
    if not files:
        print(f"[WARN] no JSON files found under: {root}", file=sys.stderr)

    rows: List[Dict[str, Any]] = []
    for fp in files:
        data = _read_json(fp)
        if not data:
            continue
        rows.append(_extract_row(fp, data, include_text=bool(args.include_text)))

    if not rows:
        return

    # Stable column order
    base_cols = list(rows[0].keys())
    all_cols = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                all_cols.append(k)
                seen.add(k)
    # Prefer base columns first, then any extras
    columns = base_cols + [c for c in all_cols if c not in base_cols]

    if args.out == "-":
        out_f = sys.stdout
        close = False
    else:
        out_f = open(args.out, "w", encoding="utf-8", newline="")
        close = True

    try:
        w = csv.DictWriter(out_f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    finally:
        if close:
            out_f.close()


if __name__ == "__main__":
    main()

