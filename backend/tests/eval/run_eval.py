"""Tiny eval runner for akb_search.

Reads JSONL samples, hits the REST search endpoint, computes per-sample
Recall@K and MRR, prints a report. Exits non-zero if aggregate recall
drops below the threshold.

Usage:
    AKB_URL=... AKB_PAT=... python run_eval.py samples.jsonl [--threshold 0.5]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import urllib.request
import urllib.parse


@dataclass
class Sample:
    query: str
    vault: str | None
    expected_titles: list[str]
    k: int = 10


def _load(path: str) -> list[Sample]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            obj = json.loads(line)
            out.append(Sample(
                query=obj["query"],
                vault=obj.get("vault"),
                expected_titles=list(obj.get("expected_titles") or []),
                k=int(obj.get("k") or 10),
            ))
    return out


def _search(base_url: str, pat: str, sample: Sample) -> list[dict[str, Any]]:
    params = {"q": sample.query, "limit": str(sample.k)}
    if sample.vault:
        params["vault"] = sample.vault
    url = base_url.rstrip("/") + "/api/v1/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {pat}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return list(data.get("results") or [])


def _score(expected: list[str], results: list[dict[str, Any]]) -> tuple[float, float]:
    if not expected:
        return (1.0, 1.0)  # No expectations → trivially pass.
    titles = [r.get("title") or "" for r in results]
    hits = sum(1 for t in expected if t in titles)
    recall = hits / len(expected)
    mrr = 0.0
    for t in expected:
        for i, rt in enumerate(titles):
            if rt == t:
                mrr = max(mrr, 1.0 / (i + 1))
                break
    return (recall, mrr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("samples_path")
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    base = os.environ.get("AKB_URL") or "http://localhost:8000"
    pat = os.environ.get("AKB_PAT") or ""
    if not pat:
        print("AKB_PAT env not set", file=sys.stderr)
        return 2

    samples = _load(args.samples_path)
    if not samples:
        print("no samples")
        return 0

    total_recall = 0.0
    total_mrr = 0.0
    failed: list[str] = []

    for s in samples:
        try:
            results = _search(base, pat, s)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{s.query}: {e}")
            continue
        recall, mrr = _score(s.expected_titles, results)
        total_recall += recall
        total_mrr += mrr
        status = "OK" if recall >= args.threshold else "LOW"
        print(f"[{status}] recall={recall:.2f} mrr={mrr:.2f}  q={s.query!r}")

    n = len(samples)
    avg_recall = total_recall / n if n else 0.0
    avg_mrr = total_mrr / n if n else 0.0
    print(f"\naggregate: recall={avg_recall:.3f} mrr={avg_mrr:.3f} samples={n} errors={len(failed)}")
    if failed:
        for f in failed:
            print("error:", f, file=sys.stderr)

    return 0 if avg_recall >= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
