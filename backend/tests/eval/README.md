# Search eval harness

Minimal quality-regression harness for `akb_search`. Each sample defines
a query + a set of document titles that MUST appear in the top-K results.

## Layout

- `samples.jsonl` — one JSON object per line: `{query, vault, expected_titles, k}`
- `run_eval.py`   — loads samples, calls `/api/v1/search`, computes Recall@K, MRR

## Running

```bash
AKB_URL=http://localhost:8000 \
AKB_PAT=<personal-access-token> \
python backend/tests/eval/run_eval.py backend/tests/eval/samples.jsonl
```

Exit code is 0 iff Recall@K >= threshold (default 0.5) for every sample.
Use it as a lightweight regression gate on search backend changes.

## Adding samples

Keep samples grounded in real docs. A sample is only useful if the expected
doc actually exists in the target vault and its title is stable. Ship a
seed set per vault (Korean keyword, English keyword, multi-token, phrase).
