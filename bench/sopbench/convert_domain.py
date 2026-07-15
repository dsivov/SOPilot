#!/usr/bin/env python3
"""Convert a SOPBench task into a SOPilot TaskDefinition (deliverable #1 of
bench/SOPBENCH_PLAN.md) — and optionally push it into a tenant as a draft SOP
so the Studio's graph view renders an external benchmark procedure.

Mapping: tool nodes → agent_actions; AND-required prerequisites of the target
service → hard ordering edges; OR groups → carried textually on the target's
description + must_say (our graph is AND-only by design). Terminal outcome
states are synthesized (approve/deny) with success/failure markers.

Usage (from the SOPilot backend venv, with sopilot importable):
  python convert_domain.py --sopbench-root <path> --domain university \
      --goal declare_minor [--index 0] [--push --base http://127.0.0.1:8100 \
      --api-key sop_... --project main]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path


def tool_descriptions(sopbench_root: Path, domain: str) -> dict[str, str]:
    """Best-effort: pull tool descriptions from the domain's assistant module."""
    try:
        sys.path.insert(0, str(sopbench_root))
        mod = __import__(f"env.domains.{domain}.{domain}_assistant", fromlist=["actions"])
        return {a["name"]: (a.get("description") or "")[:300] for a in getattr(mod, "actions", [])}
    except Exception:
        return {}


def analyze_graph(graph: dict) -> dict:
    nodes = graph["nodes"]
    kids: dict[int, list[int]] = {}
    for parent, child in graph.get("connections", []):
        kids.setdefault(parent, []).append(child)

    def is_tool(i: int) -> bool:
        return not isinstance(nodes[i], str)

    tools = [nodes[i][0] for i in range(len(nodes)) if is_tool(i)]
    target = nodes[0][0]
    required: set[str] = set()
    or_groups: list[list[str]] = []

    def walk(i: int, under_or: bool) -> None:
        node = nodes[i]
        children = kids.get(i, [])
        if is_tool(i):
            if not under_or and node[0] != target:
                required.add(node[0])
            for c in children:
                walk(c, under_or)
        elif node == "and":
            for c in children:
                walk(c, under_or)
        elif node == "or":
            alts = sorted({nodes[c][0] for c in children if is_tool(c)})
            if alts:
                or_groups.append(alts)
            for c in children:
                walk(c, True)

    for c in kids.get(0, []):
        walk(c, False)
    return {"target": target, "tools": tools, "required": sorted(required), "or_groups": or_groups}


def convert(task: dict, domain: str, goal: str, descriptions: dict[str, str]) -> dict:
    info = analyze_graph(task["directed_action_graph"])
    target = info["target"]
    actions = []
    for tool in dict.fromkeys(info["tools"]):  # preserve order, dedupe
        entry: dict = {"name": tool, "description": descriptions.get(tool, "")}
        if tool == target:
            notes = []
            if info["or_groups"]:
                for alts in info["or_groups"]:
                    notes.append("Verify at least ONE of: " + " OR ".join(alts) + ".")
            entry["description"] = (entry["description"] + " " + " ".join(notes)).strip()
            entry["must_say"] = []
        actions.append(entry)
    edges = [
        {"src": prereq, "dst": target, "direction": "forward", "note": "verification before service"}
        for prereq in info["required"]
    ]
    definition = {
        "name": f"SOPBench {domain}: {goal}",
        "description": f"Converted from SOPBench task '{goal}' ({domain} domain). "
        "External benchmark procedure — verification steps must precede the service action.",
        "conversation_profile": {
            "agent_role": f"{domain.title()} service agent",
            "goal": f"Process the user's {goal} request per SOP: verify all constraints, "
            "then execute only if permissible.",
            "success_markers": ["RequestResolved"],
            "failure_markers": ["RequestRefusedImproperly"],
        },
        "agent_actions": actions,
        "user_states": [
            {"name": "MakingRequest", "description": "User states the service they want"},
            {"name": "ProvidingInformation", "description": "User supplies identifiers/details"},
            {"name": "RequestResolved", "description": "Request executed or correctly declined — ends"},
            {"name": "RequestRefusedImproperly", "description": "Procedure abandoned — ends"},
        ],
        "data_dependencies": [],
        "sop": {"edges": edges},
    }
    return definition


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sopbench-root", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--project", default="main")
    args = ap.parse_args()

    root = Path(args.sopbench_root)
    tasks = json.loads((root / "data" / f"{args.domain}_tasks.json").read_text())
    task = tasks[args.goal][args.index]
    definition = convert(task, args.domain, args.goal, tool_descriptions(root, args.domain))

    # validate with the product's own schema + linter
    from sopilot.schemas import TaskDefinition
    from sopilot.sop_graph import SOPGraph

    task_def = TaskDefinition.model_validate(definition)
    problems = SOPGraph(task_def).lint()
    print(f"converted: {definition['name']}")
    print(f"actions: {[a['name'] for a in definition['agent_actions']]}")
    print(f"ordering edges: {len(definition['sop']['edges'])} | lint problems: {problems or 'none'}")

    if args.push:
        req = urllib.request.Request(
            args.base + "/sops",
            data=json.dumps({"definition": task_def.model_dump()}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {args.api_key}",
                "X-Project": args.project,
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())
        print(f"pushed as draft SOP id={out['id']} (view it in the Studio graph tab)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
