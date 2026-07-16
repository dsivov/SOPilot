#!/usr/bin/env python3
"""AENA dump survey: corpus shape + garbage taxonomy. Pure heuristics, no LLM.

Reads every transcript under the dump dir, emits survey.json (aggregates only —
no conversation content leaves this machine) and prints a human summary.

Filename convention observed:
  default_transcript_<start_us>_<end_us>_<uuid>_<uuid>.txt
  (microsecond epoch timestamps → call duration and date range)
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DUMP = Path(sys.argv[1] if len(sys.argv) > 1 else "/storage/Work/SOPilot/use_case/aena_dump_260716")
OUT = Path(__file__).parent / "survey.json"

TS_RE = re.compile(r"default_transcript_(\d{16})_(\d{16})_")
LINE_RE = re.compile(r"^(Client|Agent)\s*:\s*(.*)$")

# crude language cues (Spanish vs English vs other) on stopword hits
ES_WORDS = {"que", "de", "la", "el", "por", "favor", "gracias", "donde", "dónde", "está", "esta", "aquí", "usted", "hola", "vale", "bueno", "sí"}
EN_WORDS = {"the", "you", "where", "is", "thank", "thanks", "please", "hello", "yes", "flight", "gate", "how", "what"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", text).strip()


def survey_one(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    turns: list[tuple[str, str]] = []
    unparsed = 0
    for ln in lines:
        m = LINE_RE.match(ln)
        if m:
            turns.append((m.group(1), m.group(2).strip()))
        else:
            unparsed += 1

    n_client = sum(1 for s, _ in turns if s == "Client")
    n_agent = sum(1 for s, _ in turns if s == "Agent")

    # diarization echo: same normalized text appearing again within a 3-line window
    normed = [norm(t) for _, t in turns]
    echo = 0
    for i, nt in enumerate(normed):
        if len(nt) < 12:
            continue
        for j in range(max(0, i - 3), i):
            a, b = normed[j], nt
            if a and (a == b or (len(a) > 25 and (a in b or b in a))):
                echo += 1
                break

    words = norm(raw).split()
    wordset = set(words)
    es = len(wordset & ES_WORDS)
    en = len(wordset & EN_WORDS)
    lang = "es" if es >= en and es > 1 else "en" if en > es and en > 1 else "other/short"

    m = TS_RE.search(path.name)
    start_s = int(m.group(1)) / 1e6 if m else None
    dur_s = (int(m.group(2)) - int(m.group(1))) / 1e6 if m else None

    return {
        "file": str(path.relative_to(DUMP)),
        "bytes": path.stat().st_size,
        "lines": len(lines),
        "turns": len(turns),
        "unparsed_lines": unparsed,
        "client_turns": n_client,
        "agent_turns": n_agent,
        "echo_turns": echo,
        "words": len(words),
        "lang": lang,
        "start_s": start_s,
        "dur_s": dur_s,
    }


def main() -> None:
    files = sorted(DUMP.glob("*/*.txt"))
    rows = [survey_one(p) for p in files]
    n = len(rows)

    def dist(key, xs=None):
        xs = xs if xs is not None else [r[key] for r in rows if r[key] is not None]
        xs = sorted(xs)
        if not xs:
            return {}
        q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
        return {"min": xs[0], "p25": q(0.25), "p50": q(0.5), "p75": q(0.75), "p95": q(0.95), "max": xs[-1],
                "mean": round(statistics.mean(xs), 1)}

    empty = [r for r in rows if r["turns"] == 0]
    tiny = [r for r in rows if 0 < r["turns"] <= 2]
    no_agent = [r for r in rows if r["turns"] > 2 and r["agent_turns"] == 0]
    no_client = [r for r in rows if r["turns"] > 2 and r["client_turns"] == 0]
    echo_rates = [r["echo_turns"] / r["turns"] for r in rows if r["turns"] >= 4]
    heavy_echo = [r for r in rows if r["turns"] >= 4 and r["echo_turns"] / r["turns"] > 0.3]
    langs = Counter(r["lang"] for r in rows)
    dates = [datetime.fromtimestamp(r["start_s"], tz=timezone.utc).date().isoformat() for r in rows if r["start_s"]]

    summary = {
        "dump": str(DUMP),
        "dialogues": n,
        "date_range": [min(dates), max(dates)] if dates else None,
        "distinct_days": len(set(dates)),
        "turns_dist": dist("turns"),
        "words_dist": dist("words"),
        "duration_s_dist": dist("dur_s"),
        "languages": dict(langs.most_common()),
        "garbage": {
            "empty_files": len(empty),
            "tiny_leq2_turns": len(tiny),
            "no_agent_side": len(no_agent),
            "no_client_side": len(no_client),
            "heavy_echo_gt30pct": len(heavy_echo),
        },
        "echo_rate_dist": dist(None, echo_rates) if echo_rates else {},
        "unparsed_lines_total": sum(r["unparsed_lines"] for r in rows),
    }
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
