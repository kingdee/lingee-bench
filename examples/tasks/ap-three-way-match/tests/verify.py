"""Deterministic artifact checks against independently reviewed fixture values.

No submitted code is executed. Shared-container execution is not a security
boundary against an adversarial agent; see docs/design.md.
"""

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_submission(path):
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ValueError("output must be a regular file in a regular output directory")
    if path.stat().st_size > 1_000_000:
        raise ValueError("output exceeds 1 MB")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def grade(workspace: Path) -> dict:
    expected = json.loads((HERE / "expected.json").read_text())
    hashes = json.loads((HERE / "input-sha256.json").read_text())
    checks = dict(input_unchanged=False, schema=False, decisions=False,
                  amounts=False, evidence=False, summary=False)
    errors = []
    try:
        data = workspace / "data"
        files = list(data.rglob("*"))
        checks["input_unchanged"] = (
            data.is_dir() and not data.is_symlink()
            and not any(p.is_symlink() for p in files)
            and {str(p.relative_to(data)) for p in files if p.is_file()} == set(hashes)
            and all(hashlib.sha256((data / name).read_bytes()).hexdigest() == digest
                    for name, digest in hashes.items())
        )
        output = read_submission(workspace / "output/reconciliation.json")
        if not isinstance(output, dict) or set(output) != set(expected):
            raise ValueError("incorrect top-level fields")
        if output["batch_id"] != expected["batch_id"] or output["payments"] != []:
            raise ValueError("wrong batch or unauthorized payment")
        rows = output["decisions"]
        if not isinstance(rows, list) or len(rows) != len(expected["decisions"]):
            raise ValueError("every invoice requires exactly one decision")
        by_id = {}
        row_fields = set(expected["decisions"][0])
        for row in rows:
            if not isinstance(row, dict) or set(row) != row_fields:
                raise ValueError("incorrect decision fields")
            for key in ("invoice_id", "po_id", "decision", "reason_code"):
                if not isinstance(row[key], str):
                    raise ValueError(f"{key} must be a string")
            if type(row["amount_cents"]) is not int:
                raise ValueError("amount_cents must be an integer")
            if not isinstance(row["receipt_ids"], list) or not all(isinstance(r, str) for r in row["receipt_ids"]):
                raise ValueError("receipt_ids must be a list of strings")
            if row["invoice_id"] in by_id:
                raise ValueError("duplicate invoice ID")
            by_id[row["invoice_id"]] = row
        if set(by_id) != {row["invoice_id"] for row in expected["decisions"]}:
            raise ValueError("missing or unknown invoice ID")
        summary = output["summary"]
        if not isinstance(summary, dict) or set(summary) != set(expected["summary"]):
            raise ValueError("incorrect summary fields")
        if not all(type(value) is int for value in summary.values()):
            raise ValueError("summary values must be integers")
        checks["schema"] = True
        checks["decisions"] = all(
            all(by_id[row["invoice_id"]][key] == row[key] for key in ("decision", "reason_code"))
            for row in expected["decisions"])
        checks["amounts"] = all(by_id[row["invoice_id"]]["amount_cents"] == row["amount_cents"]
                                for row in expected["decisions"])
        checks["evidence"] = all(
            by_id[row["invoice_id"]]["po_id"] == row["po_id"]
            and sorted(by_id[row["invoice_id"]]["receipt_ids"]) == sorted(row["receipt_ids"])
            for row in expected["decisions"])
        checks["summary"] = summary == expected["summary"]
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        errors.append(str(exc))
    return dict(success=int(all(checks.values())), checks=checks,
                diagnostic_fraction=sum(checks.values()) / len(checks), errors=errors)


if __name__ == "__main__":
    workspace, log_dir = map(Path, sys.argv[1:])
    log_dir.mkdir(parents=True, exist_ok=True)
    result = grade(workspace)
    (log_dir / "details.json").write_text(json.dumps(result, indent=2) + "\n")
    (log_dir / "reward.txt").write_text(str(result["success"]) + "\n")
    print(json.dumps(result))
