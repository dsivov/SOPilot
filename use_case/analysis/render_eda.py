#!/usr/bin/env python3
"""Render docs/AENA_DATASET_ANALYSIS.html from eda.json + intents.json.
Pure dataset-analysis report (aggregates only; no verbatim conversation content).
House dark palette; self-contained SVG charts."""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
E = json.load((HERE / "eda.json").open())
I = json.load((HERE / "intents.json").open())["summary"]

BLUE, GREEN, AMBER, PURPLE, PINK, TEAL = "#5b8def", "#19b89a", "#f0a73c", "#a974f0", "#ef5b6e", "#3ecf8e"
CATS = [BLUE, GREEN, AMBER, PURPLE, TEAL, PINK, "#8fd9c9", "#ffce86"]


def vbars(data, w=680, h=190, color=BLUE, fmt=lambda v: str(v), title="", xlab=None):
    """data: list[(label, value)]."""
    mx = max(v for _, v in data) or 1
    n = len(data)
    bw = (w - 60) / n
    bars, labels = [], []
    for i, (lab, v) in enumerate(data):
        bh = (v / mx) * (h - 46)
        x = 48 + i * bw
        bars.append(f'<rect x="{x+bw*0.15:.1f}" y="{h-28-bh:.1f}" width="{bw*0.7:.1f}" height="{bh:.1f}" rx="2" fill="{color}"/>')
        if n <= 24:
            labels.append(f'<text x="{x+bw/2:.1f}" y="{h-14}" text-anchor="middle" style="fill:#7b8499;font:9.5px sans-serif">{lab}</text>')
        if n <= 12 or (n <= 24 and v == mx):
            labels.append(f'<text x="{x+bw/2:.1f}" y="{h-32-bh:.1f}" text-anchor="middle" style="fill:#e7ebf3;font:9.5px sans-serif;font-weight:700">{fmt(v)}</text>')
    t = f'<text x="12" y="18" style="fill:#e7ebf3;font:12.5px sans-serif;font-weight:700">{title}</text>' if title else ""
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{title}">{t}{"".join(bars)}{"".join(labels)}</svg>'


def hbars(data, w=680, rowh=26, color=None, unit="", title=""):
    """data: list[(label, value)]; horizontal."""
    mx = max(v for _, v in data) or 1
    h = 30 + len(data) * rowh
    rows = []
    for i, (lab, v) in enumerate(data):
        y = 30 + i * rowh
        bl = (v / mx) * (w - 250)
        c = color or CATS[i % len(CATS)]
        rows.append(f'<text x="12" y="{y+15}" style="fill:#aab3c5;font:12px sans-serif">{lab}</text>'
                    f'<rect x="200" y="{y+3}" width="{bl:.1f}" height="14" rx="3" fill="{c}"/>'
                    f'<text x="{206+bl:.1f}" y="{y+15}" style="fill:#e7ebf3;font:12px sans-serif;font-weight:700">{v}{unit}</text>')
    t = f'<text x="12" y="18" style="fill:#e7ebf3;font:12.5px sans-serif;font-weight:700">{title}</text>'
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{title}">{t}{"".join(rows)}</svg>'


def line(dates, counts, w=680, h=200, title=""):
    mx = max(counts) or 1
    n = len(counts)
    pts = " ".join(f"{48+i/(n-1)*(w-64):.1f},{h-28-(c/mx)*(h-50):.1f}" for i, c in enumerate(counts))
    grid = "".join(f'<line x1="48" y1="{h-28-f*(h-50):.1f}" x2="{w-16}" y2="{h-28-f*(h-50):.1f}" stroke="#2c3346"/>'
                   f'<text x="42" y="{h-24-f*(h-50):.1f}" text-anchor="end" style="fill:#7b8499;font:9px sans-serif">{int(mx*f)}</text>'
                   for f in (0, .5, 1))
    xl = "".join(f'<text x="{48+i/(n-1)*(w-64):.1f}" y="{h-12}" text-anchor="middle" style="fill:#7b8499;font:9px sans-serif">{dates[i][5:]}</text>'
                 for i in (0, n//4, n//2, 3*n//4, n-1))
    t = f'<text x="12" y="18" style="fill:#e7ebf3;font:12.5px sans-serif;font-weight:700">{title}</text>'
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{title}">{t}{grid}<polyline points="{pts}" fill="none" stroke="{BLUE}" stroke-width="2"/>{xl}</svg>'


def scatter(points, w=680, h=420, title=""):
    xs = [p[0] for p in points]; ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sx = lambda x: 30 + (x - x0) / (x1 - x0 or 1) * (w - 50)
    sy = lambda y: h - 30 - (y - y0) / (y1 - y0 or 1) * (h - 50)
    dots = "".join(f'<circle cx="{sx(p[0]):.1f}" cy="{sy(p[1]):.1f}" r="2.6" fill="{CATS[p[2]%len(CATS)]}" opacity="0.72"/>' for p in points)
    t = f'<text x="12" y="18" style="fill:#e7ebf3;font:12.5px sans-serif;font-weight:700">{title}</text>'
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{title}">{t}{dots}</svg>'


def tile(big, label, color="#9cc0ff"):
    return (f'<div class="card"><div class="cbody" style="padding:14px 16px">'
            f'<div style="font-size:27px;font-weight:800;color:{color}">{big}</div>'
            f'<div class="small" style="margin-top:2px">{label}</div></div></div>')


def _candidate_section():
    """Item-4a discovery: what actionable topics live in the ~29% not covered
    by the three current procedures (from candidate_topics.json)."""
    f = HERE / "candidate_topics.json"
    if not f.exists():
        return ""
    C = json.load(f.open())
    # consolidate the LLM cluster names into clean procedure groups
    GROUPS = [
        ("Airport services & facilities", ["airport services inquiries", "assistance with facilities",
         "airport services and information", "airport Wi-Fi assistance"], "wi-fi, ATMs, phones, pharmacies, lockers, maps — general facility questions"),
        ("Language / translation assistance", ["language assistance", "language assistance requests"],
         "travellers who need help communicating — the multilingual long tail (French, Italian, Welsh, Dutch…)"),
        ("Special assistance (reduced mobility / PMR)", ["assistance for disabled travelers"],
         "wheelchair and reduced-mobility assistance — a distinct, high-empathy procedure"),
        ("Security, check-in & documents", ["security and check-in assistance"],
         "security control, prohibited items, check-in and document questions"),
        ("Lost & found personal items", ["lost items inquiry"],
         "wallets, phones, documents left behind — the airport lost-property desk, distinct from airline baggage"),
    ]
    idx = {t["topic"].lower(): t for t in C["ranked_candidate_topics"]}
    bars = []
    for name, keys, _ in GROUPS:
        n = sum(idx.get(k.lower(), {}).get("conversations", 0) for k in keys)
        if n:
            bars.append((name, n))
    bars.sort(key=lambda x: -x[1])
    noise = C["candidate_pool"] - round(C["actionable_share_of_pool"] * C["candidate_pool"])
    rows = ""
    for name, keys, desc in sorted(GROUPS, key=lambda g: -sum(idx.get(k.lower(), {}).get("conversations", 0) for k in g[1])):
        n = sum(idx.get(k.lower(), {}).get("conversations", 0) for k in keys)
        if n:
            rows += f'<tr><td class="num">~{n}</td><td class="num">~{round(100*n/C["total"],1)}%</td><td><b>{name}</b></td><td class="small">{desc}</td></tr>'
    return f"""<section id="candidates"><div class="kicker">What's in the uncovered demand</div>
<h2>The next candidate topics</h2>
<p class="lead-p">Beyond the four dominant themes, <b>{C['candidate_pool']:,} conversations ({round(100*C['candidate_pool']/C['total'])}%)</b>
are not one of them. Clustering that pool within-language and labelling each group separates genuine service needs
from noise (greetings, closings, chit-chat, unintelligible transcripts). About <b>{round(100*C['actionable_share_of_pool'])}%</b>
of the pool is actionable demand; the rest is non-topic noise — itself a useful finding.</p>
<figure><div class="fig">{hbars(bars, unit="", title="Actionable topics in the uncovered demand (conversations)")}</div>
<figcaption>Data-driven candidate procedures, ranked by volume — a backlog grounded in the corpus rather than
intuition.</figcaption></figure>
<div class="tablewrap"><table><thead><tr><th>volume</th><th>share</th><th>candidate topic</th><th>what the traveller wants</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="small">Method: the ~{round(100*C['candidate_pool']/C['total'])}% uncovered pool embedded and K-means clustered
within Spanish and English; each cluster labelled (topic + actionable-vs-noise) from its term signature and sample
openings. Volumes are consolidated across the language-specific clusters and are conservative — the same topics also
appear inside the noise-heavy clusters. Language assistance overlaps the multilingual finding above.</p>
</section>"""


# -------- assemble sections --------
css = (ROOT / "docs/BLOG_SOPILOT_KICKOFF.html").read_text()
css = css[css.index("<style>"):css.index("</style>") + 8]

dur, trn = E["duration_s"], E["turns"]
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
topics = {"flight_info": "flight & boarding", "transport_parking": "transport & parking",
          "wayfinding": "wayfinding", "lost_luggage": "lost luggage", "security_documents": "security & docs",
          "lost_found_items": "lost & found items", "airport_services": "airport services",
          "shopping_taxfree": "shopping / tax-free"}
prim = I["primary_intent"]

dur_hist = list(zip([f"{int(E['duration_hist']['edges'][i])}" for i in range(0, len(E['duration_hist']['counts']), 3)],
                    [E['duration_hist']['counts'][i] for i in range(0, len(E['duration_hist']['counts']), 3)]))
turns_hist = list(zip([str(int(e)) for e in E['turns_hist']['edges'][:-1]], E['turns_hist']['counts']))

# duration by topic (top themes)
dbt = [(topics.get(k, k), v["median_s"]) for k, v in E["duration_by_topic"].items()
       if k in topics][:7]

lt = E["lang_topic_chi2"]
wl = E.get("within_language_clusters", {})

def cluster_rows(lang):
    w = wl.get(lang)
    if not w:
        return ""
    rows = ""
    for g in w["groups"]:
        top = ", ".join(g["top_terms"][:8])
        lex = " · ".join(f"{k}" for k in list(g["lexicon_topic_mix"])[:2])
        rows += f'<tr><td class="num">{g["size"]}</td><td class="num">{g["median_duration_s"]:.0f}s</td><td style="font-size:12.5px">{top}</td><td class="small">{lex}</td></tr>'
    return (f'<h4>Within {"Spanish" if lang=="es" else "English"} conversations '
            f'(n={w["n"]}, {w["k"]} clusters, silhouette {w["silhouette"]})</h4>'
            f'<div class="tablewrap"><table><thead><tr><th>size</th><th>median</th><th>characteristic terms</th><th>lexicon overlap</th></tr></thead><tbody>{rows}</tbody></table></div>')

html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AENA Málaga Help-Desk Corpus — Exploratory Data Analysis</title>{css}</head><body>
<header class="hero"><div class="hero-inner">
  <div class="eyebrow">AENA · Málaga Airport · dataset analysis</div>
  <h1>The Help-Desk Corpus, Explored</h1>
  <p class="lede">A standalone exploratory analysis of {E['n_dialogues']:,} real information-desk conversations
  ({I.get('dialogues','')} labelled) recorded over 90 days — demand, timing, conversation dynamics, data-driven
  topic structure, cohorts, and the statistical relationships between them. This document is about the data itself.</p>
  <div class="byline">Aggregate analysis only — no conversation content leaves the analysis environment · July 2026</div>
  <div class="hero-tags">
    <span class="tag"><b>{E['n_dialogues']:,}</b> conversations</span>
    <span class="tag">median <b>{dur['p50']:.0f}s</b> · <b>{trn['p50']:.0f}</b> turns</span>
    <span class="tag">3 languages</span>
    <span class="tag">90 days</span>
  </div>
</div></header>
<div class="layout">
<nav class="toc"><div class="tl-title">Contents</div><ol>
  <li><a href="#obj">Objectives &amp; data</a></li>
  <li><a href="#demand">Demand: what is asked</a></li>
  <li><a href="#time">Temporal patterns</a></li>
  <li><a href="#shape">Conversation shape</a></li>
  <li><a href="#dyn">Turn-taking dynamics</a></li>
  <li><a href="#stat">Statistical relationships</a></li>
  <li><a href="#cluster">Data-driven topics</a></li>
  <li><a href="#candidates">Next candidate topics</a></li>
  <li><a href="#cohort">Cohorts</a></li>
  <li><a href="#find">Findings</a></li>
</ol></nav>
<main><article>

<section id="obj"><div class="kicker">Scope</div><h2>Objectives &amp; the data</h2>
<p class="lead-p">The dataset is {E['n_dialogues']:,} cleaned, diarized (Client / Agent) transcripts of the Málaga
airport information desk, 17 April – 15 July 2026, with per-conversation timestamp, duration, language, and full
turn text. This analysis asks six questions of it: <b>what</b> travellers ask, <b>when</b> they call, how
<b>long/complex</b> the exchanges are, <b>who talks</b>, whether topics form <b>data-driven clusters</b>, and
which conversation properties <b>move together</b>.</p>
<div class="grid g3" style="margin:16px 0">
  {tile(f"{E['n_dialogues']:,}", "clean conversations analysed")}
  {tile(f"{dur['p50']:.0f} s", f"median duration (p90 {dur['p90']:.0f}s)")}
  {tile(f"{trn['p50']:.0f}", f"median turns (p90 {trn['p90']:.0f})")}
</div>
<p class="small">Languages: {", ".join(f"{k} {v}" for k,v in E['languages'].items())} (whole-conversation detection;
a French/Welsh/Catalan minority sits inside the “other/short” bucket, surfaced by the clustering below).</p>
</section>

<section id="demand"><div class="kicker">Demand</div><h2>What travellers ask</h2>
<p>A bilingual keyword lexicon labels {I['labeled']:,} of {I['dialogues']:,} conversations ({100*I['labeled']//I['dialogues']}%).
Four themes dominate:</p>
<figure><div class="fig">{hbars([(topics.get(k,k), v) for k,v in list(prim.items())[:8]], title="Primary theme per conversation")}</div>
<figcaption><b>Concentrated demand.</b> Flight/boarding, transport, wayfinding and lost luggage together account for
the large majority of labelled traffic. Every theme co-occurs most with wayfinding — whatever the question, the
answer usually ends in directions.</figcaption></figure>
</section>

<section id="time"><div class="kicker">Timing</div><h2>Temporal patterns</h2>
<figure><div class="fig">{line(E['daily_volume']['dates'], E['daily_volume']['counts'], title="Daily conversation volume (90 days)")}</div>
<figcaption>Daily volume across the window; the desk runs continuously with day-to-day variation.</figcaption></figure>
<div class="grid g2" style="margin:14px 0">
  <div class="fig">{vbars(list(zip(DOW, E['by_weekday'])), h=180, color=GREEN, title="By weekday")}</div>
  <div class="fig">{vbars([(str(i), c) for i,c in enumerate(E['by_hour_local'])], h=180, color=AMBER, title="By hour (local time)")}</div>
</div>
<p class="small">Peak hour is <b>{E['by_hour_local'].index(max(E['by_hour_local']))}:00 local</b>; the busiest days are
<b>{", ".join(DOW[i] for i in sorted(range(7), key=lambda i:-E['by_weekday'][i])[:3])}</b> — consistent with
weekend and Friday leisure-travel peaks. Staffing and agent readiness should track this shape.</p>
</section>

<section id="shape"><div class="kicker">Distributions</div><h2>Conversation shape</h2>
<div class="grid g2" style="margin:14px 0">
  <div class="fig">{vbars(dur_hist, h=190, color=BLUE, title="Duration (seconds, capped 300)")}</div>
  <div class="fig">{vbars(turns_hist, h=190, color=PURPLE, title="Turn count")}</div>
</div>
<p>Both distributions are strongly right-skewed: most exchanges are short (a single question and answer), with a
long tail of extended conversations. Duration — min {dur['min']:.0f}s, median {dur['p50']:.0f}s, p90 {dur['p90']:.0f}s,
max {dur['max']:.0f}s. Turns — median {trn['p50']:.0f}, p90 {trn['p90']:.0f}, max {trn['max']:.0f}. The typical
interaction is transactional; the tail is where confusion, language barriers, or multi-part requests live.</p>
</section>

<section id="dyn"><div class="kicker">Dynamics</div><h2>Turn-taking dynamics</h2>
<div class="grid g3" style="margin:16px 0">
  {tile(f"{100*E['agent_talk_share']['p50']:.0f}%", "median share of words spoken by the AGENT")}
  {tile(f"{E['avg_client_utterance_words']:.0f} / {E['avg_agent_utterance_words']:.0f}", "avg words per client / agent utterance")}
  {tile(f"{E['turn_balance']['client_turns']['p50']:.0f} : {E['turn_balance']['agent_turns']['p50']:.0f}", "median client : agent turns")}
</div>
<p>Agents speak a minority of the words ({100*E['agent_talk_share']['p50']:.0f}% at the median): the desk pattern is
a traveller explaining a need at some length, met by a short, directive answer (a desk, a belt, a direction). Agent
utterances are {"longer" if E['avg_agent_utterance_words']>E['avg_client_utterance_words'] else "shorter"} on average
than client utterances — {E['avg_agent_utterance_words']:.0f} vs {E['avg_client_utterance_words']:.0f} words — but
fewer in aggregate, which is the signature of concise, high-information responses.</p>
</section>

<section id="stat"><div class="kicker">Relationships</div><h2>Statistical relationships</h2>
<div class="grid g3" style="margin:16px 0">
  {tile(f"r = {E['corr_duration_turns']}", "duration ↔ turn count (Pearson) — moderate positive", GREEN)}
  {tile(f"V = {lt['cramers_v']}", f"language ↔ topic association (Cramér’s V) · χ²={lt['chi2']:.0f}, p≈{lt['p_value']:.0e}", AMBER)}
  {tile(f"r = {E['corr_clientwords_turns']}", "client words ↔ turns", BLUE)}
</div>
<p>Duration and turn count move together moderately (r = {E['corr_duration_turns']}): longer calls have more
exchanges, but not linearly — a slow single question can run as long as a brisk multi-part one. Language and topic
are <b>significantly associated</b> (χ² = {lt['chi2']:.0f}, p ≈ {lt['p_value']:.0e}) but the effect is modest
(Cramér's V = {lt['cramers_v']}): the topic mix differs by language (e.g. the balance of luggage vs transport
questions) without any language being single-topic.</p>
<figure><div class="fig">{hbars(dbt, unit="s", title="Median duration by topic (seconds)")}</div>
<figcaption>Topics differ in how long they take to resolve — a useful signal for where the desk (or any future
tooling) spends its time.</figcaption></figure>
</section>

<section id="cluster"><div class="kicker">Unsupervised structure</div><h2>Data-driven topic discovery</h2>
<p class="lead-p">Beyond the keyword lexicon, we embedded every conversation's traveller side and clustered it
(K-means, k chosen by silhouette). The result is itself a finding.</p>
<figure><div class="fig">{scatter(E.get('cluster_scatter', []), title="Conversations in embedding space (2D PCA)")}</div>
<figcaption><b>Global structure separates by language, not topic.</b> The dominant axis of variation in short
help-desk utterances is the language spoken; the low global silhouette ({E.get('cluster_silhouette','—')}) reflects
that topics do not form tight, well-separated clusters across languages — these are brief, varied, ASR-transcribed
exchanges. This is why a curated lexicon (previous section) is a more reliable topic signal here than unsupervised
clustering alone.</figcaption></figure>
<p>Clustering <i>within</i> a single language removes the language axis and surfaces interpretable sub-topics
(silhouettes remain low — the exchanges are genuinely short and overlapping — but the term signatures are clear):</p>
{cluster_rows("es")}
{cluster_rows("en")}
<p class="small">Notable: the clustering independently rediscovers the lexicon's themes (luggage, flights/check-in,
transport/parking, wayfinding) <i>and</i> isolates multilingual pockets the lexicon misses — Welsh
(“diolch/helo/iawn”), Malay/Indonesian (“saya”), and French/Italian fragments — a concrete sizing of the
long-tail-language demand.</p>
</section>

{_candidate_section()}

<section id="cohort"><div class="kicker">Segmentation</div><h2>Cohorts</h2>
<div class="grid g2" style="margin:14px 0">
  <div class="fig">{hbars(list(E['cohorts']['by_complexity'].items()), color=PURPLE, title="By complexity (turn count)")}</div>
  <div class="fig">{hbars(list(E['cohorts']['by_daypart'].items()), color=TEAL, title="By time of day")}</div>
</div>
<p>Most conversations are quick (≤4 turns); the extended (&gt;10 turn) cohort is the minority but consumes
disproportionate desk time and is where the long-tail languages and multi-part requests concentrate. Segmenting by
day-part confirms the morning-to-afternoon load the hourly chart shows.</p>
</section>

<section id="find"><div class="kicker">Takeaways</div><h2>Findings that inform future work</h2>
<ol>
  <li><b>Demand is concentrated and directional.</b> Four themes cover most traffic and nearly every answer ends in
  wayfinding — the corpus is fundamentally about routing people to places.</li>
  <li><b>Interactions are short and transactional</b> (median {dur['p50']:.0f}s, {trn['p50']:.0f} turns) with a
  heavy tail; the median exchange is one question and one directive answer.</li>
  <li><b>Load is predictable</b> — peaks at {E['by_hour_local'].index(max(E['by_hour_local']))}:00 and on
  weekends/Fridays — so capacity (human or automated) can be planned against a known shape.</li>
  <li><b>Language is the strongest axis in the raw signal</b>, and a real multilingual long tail (French, Welsh,
  Malay, Italian) exists beyond the Spanish/English majority — quantified here for the first time.</li>
  <li><b>Topic ≠ tight clusters.</b> Unsupervised clustering is language-dominated and topically soft; a curated
  lexicon labels topics more reliably, and the two agree where it matters. Any future topic model should either
  work within a language or normalise language first.</li>
  <li><b>Duration is only moderately explained by turn count</b> (r={E['corr_duration_turns']}); call length is
  driven as much by pacing/confusion as by the number of exchanges — a lever worth watching in any service metric.</li>
</ol>
</section>

</article></main></div>
<div class="footer">AENA Málaga help-desk corpus — exploratory data analysis · aggregate statistics only · the
conversation data never leaves the analysis environment.</div>
</body></html>"""

(ROOT / "docs/AENA_DATASET_ANALYSIS.html").write_text(html)
print("wrote docs/AENA_DATASET_ANALYSIS.html", len(html) // 1024, "KB")
