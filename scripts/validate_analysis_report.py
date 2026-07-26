#!/usr/bin/env python3
"""Validate a Stage-0 System Analysis report before loading it into SOPilot.

Checks the same contract the config manager enforces (see
docs/DESIGN_SYSTEM_ANALYSIS_REPORT.md and the frontend importer). No deps.

    python scripts/validate_analysis_report.py <report.json>

Exit 0 = valid (prints a summary). Exit 1 = invalid (prints the errors).
"""
import json
import sys

KIND = "sopilot-system-analysis"
FIELD_TYPES = {"string", "number", "boolean", "enum", "secret-ref", "connector-ref"}
ITEM_KINDS = {"field", "tool", "structure"}
STATUSES = {"known", "needs_input", "inferred"}


def validate(r: dict) -> list[str]:
    errs: list[str] = []
    if not isinstance(r, dict):
        return ["top level is not a JSON object"]
    if r.get("kind") != KIND:
        errs.append(f'kind must be "{KIND}" (got {r.get("kind")!r})')
    items = r.get("config_items")
    if not isinstance(items, list):
        return errs + ["config_items must be a list"]

    seen = set()
    for i, c in enumerate(items):
        where = f"config_items[{i}]"
        if not isinstance(c, dict):
            errs.append(f"{where}: not an object"); continue
        path = str(c.get("path", "")).strip()
        if not path:
            errs.append(f"{where}: missing 'path'")
        if c.get("item") not in ITEM_KINDS:
            errs.append(f"{where} ('{path}'): item must be one of {sorted(ITEM_KINDS)}")
        key = f"{c.get('item')}:{path}"
        if key in seen:
            errs.append(f"{where}: duplicate {key}")
        seen.add(key)
        if c.get("status") is not None and c.get("status") not in STATUSES:
            errs.append(f"{where} ('{path}'): status must be one of {sorted(STATUSES)}")
        if c.get("item") == "field":
            t = c.get("type")
            if t is not None and t not in FIELD_TYPES:
                errs.append(f"{where} ('{path}'): type must be one of {sorted(FIELD_TYPES)}")
            if t == "enum":
                opts = c.get("options")
                if opts is not None and not isinstance(opts, list):
                    errs.append(f"{where} ('{path}'): enum options must be a list")
                if not opts and c.get("status") != "needs_input":
                    errs.append(f"{where} ('{path}'): enum needs options (or status 'needs_input')")

    # advisory: components referenced by items should exist
    comp_ids = {c.get("id") for c in (r.get("components") or []) if isinstance(c, dict)}
    for c in items:
        cid = c.get("component") if isinstance(c, dict) else None
        if cid and comp_ids and cid not in comp_ids:
            errs.append(f"config_item '{c.get('path')}': component '{cid}' not in components[] (warning)")
    return errs


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__); return 2
    try:
        r = json.load(open(sys.argv[1]))
    except Exception as e:  # noqa: BLE001
        print(f"✖ cannot read JSON: {e}"); return 1
    errs = validate(r)
    if errs:
        print(f"✖ {len(errs)} problem(s):")
        for e in errs:
            print(f"  - {e}")
        return 1
    items = r.get("config_items", [])
    by = lambda k: sum(1 for c in items if c.get("item") == k)  # noqa: E731
    ni = [c["path"] for c in items if c.get("status") == "needs_input"]
    print(f"✔ valid — {r.get('kind')} v{r.get('report_version', '?')} for "
          f"'{(r.get('system') or {}).get('name', '?')}'")
    print(f"  {by('field')} fields · {by('tool')} tools · {by('structure')} structures "
          f"· {len(r.get('integration_points') or [])} integration points "
          f"· {len(r.get('components') or [])} components")
    if ni:
        print(f"  needs_input ({len(ni)}): {', '.join(ni)}")
    if r.get("open_questions"):
        print(f"  open_questions: {len(r['open_questions'])} (for Engineering/admin)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
