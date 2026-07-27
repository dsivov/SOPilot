// Bridge between the app-wide copilot panel and whichever view is currently
// mounted. Views publish their unsaved working object (snapshot) and register an
// apply handler for proposals; the panel reads both via refs, so publishing on
// every keystroke costs nothing (no state, no re-render cascade).
import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";

export type CopilotProposal = { kind: string; summary?: string; payload: unknown };
// An apply handler returns a short human note describing what it did (or null if
// it couldn't apply — e.g. wrong tab).
export type ApplyFn = (p: CopilotProposal) => string | null;

type Bridge = {
  getSnapshot: () => unknown;
  setSnapshot: (data: unknown) => void;
  getApply: () => ApplyFn | null;
  setApply: (fn: ApplyFn | null) => void;
};

const Ctx = createContext<Bridge | null>(null);

export function CopilotBridge({ children }: { children: ReactNode }) {
  const snap = useRef<unknown>(null);
  const apply = useRef<ApplyFn | null>(null);
  const value = useRef<Bridge>({
    getSnapshot: () => snap.current,
    setSnapshot: (d) => { snap.current = d; },
    getApply: () => apply.current,
    setApply: (f) => { apply.current = f; },
  });
  return <Ctx.Provider value={value.current}>{children}</Ctx.Provider>;
}

export function useCopilotBridge(): Bridge | null {
  return useContext(Ctx);
}

// A view calls this with its current working object; it's published to the panel
// while the view is mounted and cleared on unmount.
export function useCopilotSnapshot(data: unknown): void {
  const b = useContext(Ctx);
  useEffect(() => {
    if (!b) return;
    b.setSnapshot(data);
    return () => b.setSnapshot(null);
  }, [b, data]);
}

// A view registers how to apply a copilot proposal for its tab. The latest
// closure is always used (via a ref), so it can read current state.
export function useCopilotApply(fn: ApplyFn | null): void {
  const b = useContext(Ctx);
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!b) return;
    b.setApply((p) => (ref.current ? ref.current(p) : null));
    return () => b.setApply(null);
  }, [b]);
}
