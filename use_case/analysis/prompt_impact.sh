#!/bin/bash
set -e
cd "$(dirname "$0")"
PY=../../backend/.venv/bin/python
echo "=== RUN 1: prompts ON (comprehensive) ==="
$PY bind_prompts.py bind >/dev/null
$PY aena_ab.py 12 B
mv aena_ab_results.json aena_ab_prompts_on.json
echo "=== RUN 2: prompts OFF ==="
$PY bind_prompts.py unbind >/dev/null
$PY aena_ab.py 12 B
mv aena_ab_results.json aena_ab_prompts_off.json
echo "=== restoring prompts (bind) ==="
$PY bind_prompts.py bind >/dev/null
echo "=== COMPARISON: impact of approved-wording prompts ==="
$PY - <<'PYEOF'
import json
def agg(f):
    d=json.load(open(f)); rs=[r for r in d["results"] if r["arm"]=="B"]
    n=len(rs); cov=sum(r["coverage"]=="covered" for r in rs); par=sum(r["coverage"]=="partial" for r in rs)
    spec=sum(bool(r.get("specifics")) for r in rs); sat=sum(r.get("satisfaction",0) for r in rs)/max(1,n)
    lat=[x for r in rs for x in (r.get("latencies_ms") or [])]; lat.sort()
    p50=lat[len(lat)//2] if lat else 0
    return dict(n=n,covered=cov,partial=par,specifics=spec,satisfaction=round(sat,2),p50=p50)
on=agg("aena_ab_prompts_on.json"); off=agg("aena_ab_prompts_off.json")
print(f"{'metric':22}{'prompts OFF':>14}{'prompts ON':>14}{'delta':>10}")
for k,lab in [("covered","covered"),("partial","partial"),("specifics","specifics(concrete)"),("satisfaction","satisfaction(1-5)"),("p50","reply p50 ms")]:
    o,n=off[k],on[k]; d=(n-o)
    print(f"{lab:22}{o:>14}{n:>14}{d:>+10}")
print(f"\n(n={on['n']} scenarios per arm; SOPilot advisory, same held-out real conversations, only prompts differ)")
PYEOF
echo "=== PROMPT IMPACT DONE ==="
