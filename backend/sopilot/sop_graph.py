"""SOP graph — allowed next actions from ordering + trigger constraints.

Ported from the POC with its 2026-06-08 semantics fix (the fix that repaired the
credit-card SOP's loop pathology):
  - action→action forward edges are HARD ordering prerequisites (AND);
  - state→action forward edges are TRIGGERS, gating only actions that have no
    action prereqs (so terminal actions like ClosePolite can't fire at turn 1,
    while a missed state classification can't permanently block the flow).
"""
from __future__ import annotations

from .schemas import TaskDefinition


class SOPGraph:
    def __init__(self, task: TaskDefinition):
        self.task = task
        self.action_names = {a.name for a in task.agent_actions}
        self.state_names = {s.name for s in task.user_states}
        self.action_prereqs: dict[str, set[str]] = {a: set() for a in self.action_names}
        self.state_prereqs: dict[str, set[str]] = {a: set() for a in self.action_names}
        for e in task.sop.edges:
            if e.direction == "forward" and e.dst in self.action_names:
                if e.src in self.action_names:
                    self.action_prereqs[e.dst].add(e.src)
                elif e.src in self.state_names:
                    self.state_prereqs[e.dst].add(e.src)
            elif e.direction == "backward" and e.src in self.action_names:
                if e.dst in self.action_names:
                    self.action_prereqs[e.src].add(e.dst)
            # "both" carries no ordering constraint

    def allowed_actions(self, visited: set[str]) -> list[str]:
        out: list[str] = []
        for a in self.action_names:
            if not self.action_prereqs[a].issubset(visited):
                continue
            if self.action_prereqs[a]:
                out.append(a)  # ordering satisfied
            elif not self.state_prereqs[a]:
                out.append(a)  # no prereqs at all
            elif self.state_prereqs[a] & visited:
                out.append(a)  # state-triggered and the trigger occurred
        if not out:
            return sorted(self.action_names)  # never strand the agent
        return sorted(out)

    def visited_from_history(self, history: list[dict[str, str]], state_log: list[str]) -> set[str]:
        visited: set[str] = set()
        for h in history:
            tag = h.get("action")
            if tag and tag in self.action_names:
                visited.add(tag)
        for s in state_log:
            if s in self.state_names:
                visited.add(s)
        return visited

    # ---------- Lint (publish blockers — the credit-card lesson) ----------

    def lint(self) -> list[str]:
        """Structural checks run before an SOP version can be published."""
        problems: list[str] = []
        node_names = self.action_names | self.state_names
        for e in self.task.sop.edges:
            for endpoint in (e.src, e.dst):
                if endpoint not in node_names:
                    problems.append(f"edge {e.src}->{e.dst}: unknown node '{endpoint}'")
        # Cycle detection over hard ordering prereqs (a cycle deadlocks the flow).
        color: dict[str, int] = {}

        def dfs(a: str) -> bool:
            color[a] = 1
            for p in self.action_prereqs.get(a, ()):  # edge p -> a
                c = color.get(p, 0)
                if c == 1 or (c == 0 and dfs(p)):
                    return True
            color[a] = 2
            return False

        for a in self.action_names:
            if color.get(a, 0) == 0 and dfs(a):
                problems.append(f"ordering cycle involving action '{a}'")
                break
        # Reachability: every action must be executable in SOME order. State triggers
        # are runtime events assumed satisfiable, so only ordering prereqs gate this.
        reachable: set[str] = set()
        frontier = True
        while frontier:
            frontier = False
            for a in self.action_names - reachable:
                if self.action_prereqs[a].issubset(reachable):
                    reachable.add(a)
                    frontier = True
        for a in sorted(self.action_names - reachable):
            problems.append(f"action '{a}' unreachable (unsatisfiable ordering prereqs)")
        # Terminal markers must be declared user states.
        cp = self.task.conversation_profile
        for marker in list(cp.success_markers) + list(cp.failure_markers):
            if marker not in self.state_names:
                problems.append(f"terminal marker '{marker}' is not a declared user_state")
        # Data dependencies referenced by actions must exist.
        declared = {d.name for d in self.task.data_dependencies}
        for a in self.task.agent_actions:
            for dep in a.data_dependencies:
                if dep not in declared:
                    problems.append(f"action '{a.name}' references undeclared data dependency '{dep}'")
        return problems
