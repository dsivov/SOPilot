#!/usr/bin/env python3
"""Full exploratory data analysis of the AENA Málaga help-desk corpus.

Pure dataset analysis — demand, timing, conversation dynamics, data-driven
topic clustering, cohorts, and statistical relationships. Emits eda.json
(AGGREGATES ONLY — no raw conversation content leaves the machine). Clustering
uses OpenAI embeddings; everything else is local. Requires: numpy, scikit-learn, scipy (analysis toolkit — not the product runtime).

Usage: backend/.venv/bin/python eda.py            # full run incl. clustering
       backend/.venv/bin/python eda.py --no-cluster
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
LOCAL_OFFSET_H = 2  # Europe/Madrid = UTC+2 (CEST) across the Apr–Jul window
rng = np.random.default_rng(7)


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


STOP = set(norm(w) for w in (
    # articles/pronouns/prepositions/copulas (es + en)
    "que de la el los las un una y a en es no si por para con me se lo le mi su al del pero como mas o ya esta "
    "esto ese esa hay tiene tengo tienes usted le les nos te te un unos unas este estos estas ese esos esas "
    "the a an and or is are was were you i to of in it that this for on at my your be been being have has had "
    "he she they we them his her our their there here me him us do does did can could would should will "
    # discourse markers / fillers / greetings / closings (es/en/welsh/french) — remove so TOPIC terms surface
    "muy bien vale gracias hola buenos dias tardes favor perdon pues bueno claro vamos ahi ahora oye escucha "
    "venga adios apunta mira digo estoy luego entonces buenas muchas oiga eso asi aqui alli alla dentro donde "
    "cuando quien porque tambien solo cada todo todos toda nada algo mismo cosa hombre senor senora vez "
    "yeah right just need very much well got know see look okay ok so please thank thanks hello hi bye "
    "from here what how where when why who which about would like want going get one two also only every "
    "diolch helo iawn nos da bore pnawn vous merci pour mais oui non bonjour au revoir eh em ah uh hmm mm "
    "que si no more still even really actually maybe perhaps things thing something anything nothing everyone").split())


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2) if xs else 0


def dist(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {}
    return {"n": len(xs), "min": round(min(xs), 2), "p10": pct(xs, .1), "p25": pct(xs, .25),
            "p50": pct(xs, .5), "p75": pct(xs, .75), "p90": pct(xs, .9), "p95": pct(xs, .95),
            "max": round(max(xs), 2), "mean": round(float(np.mean(xs)), 2), "std": round(float(np.std(xs)), 2)}


def histogram(xs, bins):
    xs = np.array([x for x in xs if x is not None], float)
    counts, edges = np.histogram(xs, bins=bins)
    return {"edges": [round(float(e), 1) for e in edges], "counts": [int(c) for c in counts]}


def pearson(a, b):
    a, b = np.array(a, float), np.array(b, float)
    m = ~(np.isnan(a) | np.isnan(b))
    if m.sum() < 3:
        return None
    return round(float(np.corrcoef(a[m], b[m])[0, 1]), 3)


def chi_square(table):
    """table: dict[row][col] -> count. chi2 + p-value (scipy) + Cramér's V."""
    from scipy.stats import chi2_contingency
    rows = sorted(table)
    cols = sorted({c for r in table.values() for c in r})
    M = np.array([[table[r].get(c, 0) for c in cols] for r in rows], float)
    if M.sum() == 0:
        return None
    chi2, p, dof, _ = chi2_contingency(M)
    n = M.sum()
    v = math.sqrt(chi2 / (n * (min(M.shape) - 1))) if min(M.shape) > 1 else 0
    return {"chi2": round(float(chi2), 1), "p_value": float(f"{p:.2e}"), "dof": int(dof),
            "cramers_v": round(v, 3), "n": int(n)}


def main():
    rows = [json.loads(ln) for ln in (HERE / "cleaned.jsonl").open(encoding="utf-8")]
    per_intent = {d["id"]: (d["labels"][0] if d["labels"] else "unmatched")
                  for d in json.load((HERE / "intents.json").open())["per_dialogue"]}

    # ---- derive per-dialogue features
    for r in rows:
        cl = [t["t"] for t in r["turns"] if t["s"] == "Client"]
        ag = [t["t"] for t in r["turns"] if t["s"] == "Agent"]
        r["_client_words"] = sum(len(t.split()) for t in cl)
        r["_agent_words"] = sum(len(t.split()) for t in ag)
        r["_n_client"] = len(cl)
        r["_n_agent"] = len(ag)
        r["_client_text"] = " ".join(cl)[:1200]
        r["_topic"] = per_intent.get(r["id"], "unmatched")
        try:
            dt = datetime.fromisoformat(r["start_iso"]) + timedelta(hours=LOCAL_OFFSET_H)
            r["_date"] = dt.date().isoformat()
            r["_dow"] = dt.weekday()  # 0=Mon
            r["_hour"] = dt.hour
        except Exception:
            r["_date"] = r["_dow"] = r["_hour"] = None

    out = {"n_dialogues": len(rows)}

    # ---- univariate
    out["duration_s"] = dist([r["dur_s"] for r in rows])
    out["duration_hist"] = histogram([min(r["dur_s"], 300) for r in rows if r["dur_s"]], 30)
    out["turns"] = dist([r["stats"]["turns"] for r in rows])
    out["turns_hist"] = histogram([r["stats"]["turns"] for r in rows], range(0, 40, 2))
    out["client_words"] = dist([r["_client_words"] for r in rows])
    out["agent_words"] = dist([r["_agent_words"] for r in rows])
    out["languages"] = dict(Counter(r["lang"] for r in rows).most_common())

    # ---- conversation dynamics
    ratios = [r["_agent_words"] / max(1, r["_client_words"] + r["_agent_words"]) for r in rows]
    out["agent_talk_share"] = dist(ratios)
    out["turn_balance"] = {"client_turns": dist([r["_n_client"] for r in rows]),
                           "agent_turns": dist([r["_n_agent"] for r in rows])}
    out["avg_client_utterance_words"] = round(float(np.mean(
        [r["_client_words"] / max(1, r["_n_client"]) for r in rows])), 2)
    out["avg_agent_utterance_words"] = round(float(np.mean(
        [r["_agent_words"] / max(1, r["_n_agent"]) for r in rows])), 2)

    # ---- temporal
    daily = Counter(r["_date"] for r in rows if r["_date"])
    dates = sorted(daily)
    out["daily_volume"] = {"dates": dates, "counts": [daily[d] for d in dates]}
    dow = Counter(r["_dow"] for r in rows if r["_dow"] is not None)
    out["by_weekday"] = [dow.get(i, 0) for i in range(7)]  # Mon..Sun
    hour = Counter(r["_hour"] for r in rows if r["_hour"] is not None)
    out["by_hour_local"] = [hour.get(i, 0) for i in range(24)]

    # ---- statistical relationships
    dur = [r["dur_s"] for r in rows]
    trn = [r["stats"]["turns"] for r in rows]
    out["corr_duration_turns"] = pearson(dur, trn)
    out["corr_logdur_turns"] = pearson([math.log(d + 1) for d in dur], trn)
    out["corr_clientwords_turns"] = pearson([r["_client_words"] for r in rows], trn)
    # language × topic contingency + chi2
    ct = {}
    for r in rows:
        ct.setdefault(r["lang"], Counter())[r["_topic"]] += 1
    out["lang_topic_chi2"] = chi_square({k: dict(v) for k, v in ct.items()})
    out["lang_topic_table"] = {k: dict(v) for k, v in ct.items()}
    # duration by topic / by language
    def by_group(keyfn):
        g = {}
        for r in rows:
            g.setdefault(keyfn(r), []).append(r["dur_s"])
        return {k: {"n": len(v), "median_s": pct(v, .5), "mean_s": round(float(np.mean(v)), 1)}
                for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))}
    out["duration_by_topic"] = by_group(lambda r: r["_topic"])
    out["duration_by_language"] = by_group(lambda r: r["lang"])

    # ---- cohorts
    def complexity(r):
        t = r["stats"]["turns"]
        return "quick (≤4 turns)" if t <= 4 else "standard (5–10)" if t <= 10 else "extended (>10)"
    def daypart(r):
        h = r["_hour"]
        return "unknown" if h is None else "early (0–7)" if h < 8 else "morning (8–12)" if h < 13 \
            else "afternoon (13–18)" if h < 19 else "evening (19–23)"
    out["cohorts"] = {
        "by_complexity": dict(Counter(complexity(r) for r in rows).most_common()),
        "by_daypart": dict(Counter(daypart(r) for r in rows).most_common()),
        "complexity_x_topic": {},
    }
    cx = {}
    for r in rows:
        cx.setdefault(complexity(r), Counter())[r["_topic"]] += 1
    out["cohorts"]["complexity_x_topic"] = {k: dict(v.most_common(5)) for k, v in cx.items()}

    # ---- data-driven topic clustering (embeddings + KMeans)
    if "--no-cluster" not in sys.argv:
        for line in (HERE.parent.parent / "backend/.env").read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        from openai import OpenAI
        client = OpenAI()
        texts = [r["_client_text"] or "(empty)" for r in rows]
        embs = []
        for i in range(0, len(texts), 512):
            embs.extend(d.embedding for d in client.embeddings.create(
                model="text-embedding-3-small", input=texts[i:i + 512]).data)
        X = np.array(embs, float)
        from sklearn.cluster import KMeans
        from sklearn.decomposition import PCA
        from sklearn.metrics import silhouette_score
        # choose k by silhouette (sampled) + record the elbow (inertia) curve
        elbow, sils = {}, {}
        samp = rng.choice(len(X), size=min(1500, len(X)), replace=False)
        for k in range(3, 13):
            km = KMeans(n_clusters=k, n_init=4, random_state=7).fit(X)
            elbow[k] = round(float(km.inertia_), 1)
            sils[k] = round(float(silhouette_score(X[samp], km.labels_[samp])), 3)
        out["cluster_elbow"] = elbow
        out["cluster_silhouette_by_k"] = sils
        K = max(sils, key=sils.get)
        km = KMeans(n_clusters=K, n_init=10, random_state=7).fit(X)
        labels = km.labels_
        out["cluster_k"] = int(K)
        out["cluster_silhouette"] = sils[K]
        # 2D PCA projection of the cluster centroids (for the scatter figure)
        p2 = PCA(n_components=2, random_state=7).fit(X)
        cen2 = p2.transform(km.cluster_centers_)
        out["cluster_pca_var"] = [round(float(v), 3) for v in p2.explained_variance_ratio_]
        clusters = []
        for j in range(K):
            members = [rows[i] for i in range(len(rows)) if labels[i] == j]
            if not members:
                continue
            terms = Counter()
            for r in members:
                terms.update(w for w in norm(r["_client_text"]).split() if w not in STOP and len(w) > 3)
            clusters.append({
                "id": int(j), "size": len(members), "share": round(len(members) / len(rows), 3),
                "top_terms": [t for t, _ in terms.most_common(12)],
                "median_duration_s": pct([r["dur_s"] for r in members], .5),
                "median_turns": pct([r["stats"]["turns"] for r in members], .5),
                "languages": dict(Counter(r["lang"] for r in members).most_common()),
                "lexicon_topic_mix": dict(Counter(r["_topic"] for r in members).most_common(4)),
                "centroid2d": [round(float(cen2[j][0]), 2), round(float(cen2[j][1]), 2)],
            })
        out["clusters"] = sorted(clusters, key=lambda c: -c["size"])
        # downsampled 2D point cloud for the scatter figure (aggregate coords only)
        allp = p2.transform(X)
        ds = rng.choice(len(X), size=min(1400, len(X)), replace=False)
        out["cluster_scatter"] = [[round(float(allp[i][0]), 2), round(float(allp[i][1]), 2), int(labels[i])]
                                  for i in ds]
        out["cluster_note"] = ("Global embeddings separate primarily by language; the low silhouette reflects "
                               "short, linguistically varied utterances rather than absent structure. Topic "
                               "sub-structure is clearest WITHIN a language — below.")

        # ---- within-language topic discovery (the informative view)
        Xnp = X
        wl = {}
        for lang in ("es", "en"):
            idxs = [i for i, r in enumerate(rows) if r["lang"] == lang]
            if len(idxs) < 100:
                continue
            Xl = Xnp[idxs]
            sl = {}
            sub = rng.choice(len(Xl), size=min(1200, len(Xl)), replace=False)
            for k in range(4, 11):
                kmk = KMeans(n_clusters=k, n_init=4, random_state=7).fit(Xl)
                sl[k] = float(silhouette_score(Xl[sub], kmk.labels_[sub]))
            Kk = max(sl, key=sl.get)
            kml = KMeans(n_clusters=Kk, n_init=10, random_state=7).fit(Xl)
            groups = []
            for j in range(Kk):
                mem = [rows[idxs[t]] for t in range(len(idxs)) if kml.labels_[t] == j]
                terms = Counter()
                for r in mem:
                    terms.update(w for w in norm(r["_client_text"]).split() if w not in STOP and len(w) > 3)
                groups.append({"size": len(mem), "share": round(len(mem) / len(idxs), 3),
                               "top_terms": [t for t, _ in terms.most_common(10)],
                               "median_duration_s": pct([r["dur_s"] for r in mem], .5),
                               "lexicon_topic_mix": dict(Counter(r["_topic"] for r in mem).most_common(3))})
            wl[lang] = {"n": len(idxs), "k": int(Kk), "silhouette": round(sl[Kk], 3),
                        "groups": sorted(groups, key=lambda g: -g["size"])}
        out["within_language_clusters"] = wl

    (HERE / "eda.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("eda.json written ·", out["n_dialogues"], "dialogues")
    print("duration p50/p90:", out["duration_s"]["p50"], "/", out["duration_s"]["p90"], "s")
    print("corr duration~turns:", out["corr_duration_turns"])
    if "clusters" in out:
        print(f"clusters k={out['cluster_k']} silhouette={out['cluster_silhouette']}:")
        for c in out["clusters"]:
            print(f"  [{c['size']:4}] {', '.join(c['top_terms'][:6])}")


if __name__ == "__main__":
    main()
