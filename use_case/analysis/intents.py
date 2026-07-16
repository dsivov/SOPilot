#!/usr/bin/env python3
"""Intent mining over the cleaned AENA corpus (LOCAL ONLY).

Bilingual (es/en) keyword lexicon over airport help-desk themes; multi-label
per dialogue, scored on CLIENT turns primarily (what was asked), with agent
turns at half weight (what was answered). Emits intents.json (aggregates) and
prints the distribution + lexicon-coverage check (top tokens of unmatched
dialogues, so we can see what the lexicon misses).
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent

LEXICON: dict[str, list[str]] = {
    "lost_luggage": [
        "maleta", "equipaje", "luggage", "baggage", "suitcase", "extraviado", "ground force", "groundforce",
        "cinta", "belt", "facturada", "mochila",
    ],
    "lost_found_items": [
        "objetos perdidos", "lost and found", "lost property", "cartera", "wallet", "movil", "telefono",
        "phone", "olvidado", "olvidada", "dejado", "dejada", "left my", "perdido un", "perdido una", "he perdido",
        "i lost", "llaves", "keys", "bolso", "abrigo", "dni perdido",
    ],
    "flight_info": [
        "vuelo", "flight", "puerta de embarque", "gate", "embarque", "boarding", "retraso", "delay", "delayed",
        "cancelado", "cancelled", "salida", "departure", "llegada", "arrival", "escala", "facturacion",
        "check in", "check-in", "mostrador", "counter", "tarjeta de embarque", "boarding pass",
    ],
    "transport_parking": [
        "taxi", "autobus", "bus", "tren", "train", "cercanias", "transfer", "uber", "cabify",
        "parking", "aparcamiento", "coche", "rent a car", "rental", "alquiler", "shuttle", "estacion",
    ],
    "wayfinding": [
        "donde esta", "donde estan", "donde hay", "where is", "where are", "como llego", "how do i get",
        "terminal", "salida", "exit", "bano", "banos", "aseo", "aseos", "toilet", "restroom", "servicios",
        "farmacia", "pharmacy", "cajero", "atm", "cafeteria", "planta", "floor", "nivel", "ascensor", "elevator",
        "puerta", "llegadas", "arrivals", "departures",
    ],
    "airport_services": [
        "wifi", "wi-fi", "silla de ruedas", "wheelchair", "pmr", "asistencia", "assistance", "carrito",
        "trolley", "cart", "consigna", "locker", "fumar", "smoking", "sala vip", "lounge", "informacion turistica",
        "capilla", "lactancia", "mascota", "pet",
    ],
    "security_documents": [
        "seguridad", "security", "control", "aduana", "customs", "visado", "visa", "pasaporte", "passport",
        "dni", "policia", "police", "guardia civil", "denuncia",
    ],
    "shopping_taxfree": [
        "tax free", "taxfree", "devolucion", "refund", "tienda", "shop", "duty free", "compra",
    ],
}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", text)).strip()


STOP = set(norm(w) for w in (
    "que de la el los las un una y a en es no si por para con me se lo le mi su al del pero como mas o ya "
    "esta esto ese esa aqui alli hay muy bien vale gracias hola buenos dias tardes por favor perdon "
    "the a an and or is are you i to of in it that this for on at my be have has where what how yes no "
    "please thank thanks hello ok okay so do does can could would tienes tiene tengo hay usted"
).split())


def main() -> None:
    rows = [json.loads(ln) for ln in (HERE / "cleaned.jsonl").open(encoding="utf-8")]
    lex_norm = {k: [norm(t) for t in v] for k, v in LEXICON.items()}

    label_counts: Counter[str] = Counter()
    label_cooc: Counter[tuple[str, str]] = Counter()
    unmatched_tokens: Counter[str] = Counter()
    unmatched = 0
    per_dialogue_labels = []

    for r in rows:
        client_text = norm(" ".join(t["t"] for t in r["turns"] if t["s"] == "Client"))
        agent_text = norm(" ".join(t["t"] for t in r["turns"] if t["s"] == "Agent"))
        scores: Counter[str] = Counter()
        for label, terms in lex_norm.items():
            sc = sum(2 for t in terms if t in client_text) + sum(1 for t in terms if t in agent_text)
            if sc >= 2:
                scores[label] = sc
        labels = [l for l, _ in scores.most_common(3)]
        per_dialogue_labels.append({"id": r["id"], "labels": labels, "lang": r["lang"], "dur_s": r["dur_s"]})
        if labels:
            label_counts.update(labels)
            if len(labels) > 1:
                for i in range(len(labels)):
                    for j in range(i + 1, len(labels)):
                        label_cooc[tuple(sorted((labels[i], labels[j])))] += 1
        else:
            unmatched += 1
            unmatched_tokens.update(w for w in client_text.split() if w not in STOP and len(w) > 3)

    n = len(rows)
    primary = Counter(d["labels"][0] for d in per_dialogue_labels if d["labels"])
    result = {
        "dialogues": n,
        "labeled": n - unmatched,
        "unmatched": unmatched,
        "primary_intent": dict(primary.most_common()),
        "any_intent": dict(label_counts.most_common()),
        "top_cooccurrence": {f"{a}+{b}": c for (a, b), c in label_cooc.most_common(8)},
        "unmatched_top_tokens": dict(unmatched_tokens.most_common(30)),
    }
    (HERE / "intents.json").write_text(json.dumps({"summary": result, "per_dialogue": per_dialogue_labels}, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
