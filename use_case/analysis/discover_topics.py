#!/usr/bin/env python3
"""ROADMAP item 4a — discover the next candidate procedures.

Cluster the conversations NOT covered by the three existing SOPs (the minor
lexicon themes + the unmatched bucket), WITHIN language (the EDA showed global
clustering is language-dominated), then LLM-label each cluster to separate
genuine service topics from greeting/noise. Aggregate + rank by volume →
a data-driven backlog of next procedures.

Aggregates only in the output (candidate_topics.json); sample utterances are
used locally for LLM labelling and are not written out verbatim.

Usage: backend/.venv/bin/python discover_topics.py
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np
from sklearn.cluster import KMeans

HERE = Path(__file__).parent
for line in (HERE.parent.parent / "backend/.env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
from openai import OpenAI  # noqa: E402

client = OpenAI()
rng = np.random.default_rng(7)
COVERED = {"flight_info", "transport_parking", "wayfinding", "lost_luggage"}
K_PER_LANG = {"es": 10, "en": 7}


def norm(t):
    t = unicodedata.normalize("NFKD", t.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", "".join(c for c in t if not unicodedata.combining(c)))).strip()


STOP = set(norm(w) for w in (
    "que de la el los las un una y a en es no si por para con me se lo le mi su al del pero como mas o ya esta the "
    "a an and or is are you i to of in it that this for on at my be have where what how yes please thank hello hi "
    "muy bien vale gracias hola buenos dias tardes favor perdon pues bueno claro vamos ahora venga adios mira "
    "yeah right just need very much well know see look okay so from here would like want going get diolch helo "
    "there they we them here eh em ah uh donde cuando tambien solo todo nada algo si no more still really").split())

LABEL_SYS = (
    "You review a CLUSTER of airport information-desk conversation openings (Málaga; noisy speech-to-text, "
    "Spanish/English) that an existing keyword classifier did NOT assign to one of these already-handled topics: "
    "lost luggage, flight/boarding info, transport/parking/wayfinding. Identify what THIS cluster is about.\n"
    'Return JSON {"topic": "<3-5 word service-topic name>", '
    '"actionable": true|false  (true = a real recurring traveller NEED an agent resolves; '
    'false = greetings/closings/thanks only, unintelligible ASR, wrong-number, or pure chitchat with no request), '
    '"description": "<one line: what the traveller wants>"}.'
)


def load_pool():
    per = {d["id"]: (d["labels"][0] if d["labels"] else "unmatched")
           for d in json.load((HERE / "intents.json").open())["per_dialogue"]}
    rows = [json.loads(l) for l in (HERE / "cleaned.jsonl").open(encoding="utf-8")]
    for r in rows:
        r["_client"] = " ".join(t["t"] for t in r["turns"] if t["s"] == "Client")[:800]
        r["_lex"] = per.get(r["id"], "unmatched")
    return [r for r in rows if r["_lex"] not in COVERED], len(rows)


def embed(texts):
    out = []
    for i in range(0, len(texts), 512):
        out.extend(d.embedding for d in client.embeddings.create(
            model="text-embedding-3-small", input=[t or "(empty)" for t in texts[i:i + 512]]).data)
    return np.array(out, float)


def label_cluster(terms, samples):
    body = ("Characteristic terms: " + ", ".join(terms) + "\n\nExample opening lines:\n"
            + "\n".join(f"- {s[:160]}" for s in samples))
    r = client.chat.completions.create(model="gpt-4o-mini", temperature=0, max_tokens=120,
                                       response_format={"type": "json_object"},
                                       messages=[{"role": "system", "content": LABEL_SYS},
                                                 {"role": "user", "content": body}])
    try:
        return json.loads(r.choices[0].message.content)
    except json.JSONDecodeError:
        return {"topic": "unlabeled", "actionable": False, "description": ""}


def main():
    pool, total = load_pool()
    print(f"candidate pool: {len(pool)} / {total} conversations not covered by the 3 SOPs")
    found = []
    for lang, k in K_PER_LANG.items():
        sub = [r for r in pool if r["lang"] == lang]
        if len(sub) < k * 8:
            continue
        X = embed([r["_client"] for r in sub])
        labels = KMeans(n_clusters=k, n_init=10, random_state=7).fit(X).labels_
        print(f"\n[{lang}] {len(sub)} conversations → {k} clusters")
        for j in range(k):
            mem = [sub[i] for i in range(len(sub)) if labels[i] == j]
            if len(mem) < 8:
                continue
            terms = Counter()
            for r in mem:
                terms.update(w for w in norm(r["_client"]).split() if w not in STOP and len(w) > 3)
            top = [t for t, _ in terms.most_common(10)]
            samples = [r["_client"] for r in rng.choice(mem, size=min(6, len(mem)), replace=False)]
            lab = label_cluster(top, samples)
            med = sorted(r["dur_s"] for r in mem)[len(mem) // 2]
            lex = dict(Counter(r["_lex"] for r in mem).most_common(3))
            found.append({"lang": lang, "size": len(mem), "topic": lab["topic"],
                          "actionable": bool(lab.get("actionable")), "description": lab.get("description", ""),
                          "median_duration_s": round(med, 1), "top_terms": top, "lexicon_overlap": lex})
            flag = "✓" if lab.get("actionable") else "·noise"
            print(f"  [{len(mem):3}] {flag:6} {lab['topic']:32} | {', '.join(top[:6])}")

    # ---- aggregate actionable topics across languages by normalized name
    actionable = [f for f in found if f["actionable"]]
    agg = {}
    for f in actionable:
        key = norm(f["topic"])
        a = agg.setdefault(key, {"topic": f["topic"], "conversations": 0, "languages": Counter(),
                                 "descriptions": [], "median_s": []})
        a["conversations"] += f["size"]
        a["languages"][f["lang"]] += f["size"]
        a["descriptions"].append(f["description"])
        a["median_s"].append(f["median_duration_s"])
    ranked = sorted(agg.values(), key=lambda a: -a["conversations"])
    for a in ranked:
        a["languages"] = dict(a["languages"])
        a["median_duration_s"] = round(float(np.median(a["median_s"])), 1)
        a["description"] = max(a["descriptions"], key=len)
        del a["median_s"], a["descriptions"]

    result = {
        "candidate_pool": len(pool), "total": total,
        "actionable_share_of_pool": round(sum(a["conversations"] for a in ranked) / len(pool), 3),
        "ranked_candidate_topics": ranked,
        "all_clusters": sorted(found, key=lambda f: -f["size"]),
    }
    (HERE / "candidate_topics.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("\n=== RANKED CANDIDATE PROCEDURES (actionable, by volume) ===")
    for a in ranked:
        est = round(a["conversations"] / total * 100, 1)
        print(f"  {a['conversations']:4} conv (~{est}% of all) · {a['topic']} — {a['description'][:70]}")
    print(f"\ncandidate_topics.json written · {len(ranked)} distinct actionable topics")


if __name__ == "__main__":
    main()
