#!/usr/bin/env python3
"""AENA cleaning pipeline: raw ASR transcripts → cleaned.jsonl (LOCAL ONLY).

Garbage handling (from the survey's defect taxonomy):
  drop   — empty files, dialogues with ≤2 turns, single-sided dialogues
  repair — diarization echo: the same utterance emitted twice within a short
           window (usually once per speaker label). We keep the FIRST
           occurrence and drop the later near-duplicates. Containment counts
           as duplication for long utterances (ASR often re-emits a superset).
  merge  — consecutive turns from the same speaker become one turn (ASR splits
           mid-sentence); makes turn counts meaningful for analysis.

Output: use_case/analysis/cleaned.jsonl — one dialogue per line:
  {id, file, start_iso, dur_s, lang, turns: [{s, t}], stats: {...}}
Plus clean_report.json (aggregates only — safe to share, still not committed).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

DUMP = Path(sys.argv[1] if len(sys.argv) > 1 else "/storage/Work/SOPilot/use_case/aena_dump_260716")
OUTDIR = Path(__file__).parent
LINE_RE = re.compile(r"^(Client|Agent)\s*:\s*(.*)$")
TS_RE = re.compile(r"default_transcript_(\d{16})_(\d{16})_")

ES_WORDS = {"que", "de", "la", "el", "por", "favor", "gracias", "donde", "dónde", "esta", "aqui", "usted", "hola", "vale", "bueno", "si"}
EN_WORDS = {"the", "you", "where", "is", "thank", "thanks", "please", "hello", "yes", "flight", "gate", "how", "what"}

ECHO_WINDOW = 3
MIN_TURNS = 3


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text)).strip()


def dedup_echo(turns: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], int]:
    kept: list[tuple[str, str]] = []
    kept_norm: list[str] = []
    removed = 0
    for speaker, text in turns:
        nt = norm(text)
        dup = False
        if len(nt) >= 12:
            for prev in kept_norm[-ECHO_WINDOW:]:
                if prev == nt or (len(prev) > 25 and (prev in nt or nt in prev)):
                    dup = True
                    break
        if dup:
            removed += 1
            continue
        kept.append((speaker, text))
        kept_norm.append(nt)
    return kept, removed


def merge_runs(turns: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for speaker, text in turns:
        if out and out[-1][0] == speaker:
            out[-1] = (speaker, out[-1][1] + " " + text)
        else:
            out.append((speaker, text))
    return out


def detect_lang(text: str) -> str:
    ws = set(norm(text).split())
    es, en = len(ws & ES_WORDS), len(ws & EN_WORDS)
    return "es" if es >= en and es > 1 else "en" if en > es and en > 1 else "other/short"


def main() -> None:
    files = sorted(DUMP.glob("*/*.txt"))
    kept_rows = 0
    dropped = {"empty": 0, "tiny": 0, "one_sided": 0}
    echo_removed_total = 0
    turns_before = 0
    turns_after = 0

    with (OUTDIR / "cleaned.jsonl").open("w", encoding="utf-8") as out:
        for path in files:
            raw = path.read_text(encoding="utf-8", errors="replace")
            turns = [(m.group(1), m.group(2).strip()) for ln in raw.splitlines() if (m := LINE_RE.match(ln))]
            turns = [(s, t) for s, t in turns if t]
            turns_before += len(turns)
            if not turns:
                dropped["empty"] += 1
                continue

            turns, removed = dedup_echo(turns)
            echo_removed_total += removed
            turns = merge_runs(turns)

            if len(turns) < MIN_TURNS:
                dropped["tiny"] += 1
                continue
            speakers = {s for s, _ in turns}
            if len(speakers) < 2:
                dropped["one_sided"] += 1
                continue

            turns_after += len(turns)
            m = TS_RE.search(path.name)
            start_s = int(m.group(1)) / 1e6 if m else None
            dur_s = round((int(m.group(2)) - int(m.group(1))) / 1e6, 1) if m else None
            full_text = " ".join(t for _, t in turns)
            kept_rows += 1
            out.write(
                json.dumps(
                    {
                        "id": path.parent.name,
                        "file": str(path.relative_to(DUMP)),
                        "start_iso": datetime.fromtimestamp(start_s, tz=timezone.utc).isoformat() if start_s else None,
                        "dur_s": dur_s,
                        "lang": detect_lang(full_text),
                        "turns": [{"s": s, "t": t} for s, t in turns],
                        "stats": {"echo_removed": removed, "turns": len(turns)},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    report = {
        "input_files": len(files),
        "kept_dialogues": kept_rows,
        "dropped": dropped,
        "dropped_total": sum(dropped.values()),
        "echo_turns_removed": echo_removed_total,
        "turns_before_clean": turns_before,
        "turns_after_clean_merge": turns_after,
    }
    (OUTDIR / "clean_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
