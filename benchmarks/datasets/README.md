# Dataset provenance

Source downloads are ignored and never redistributed with the MIT package.
Create `benchmarks/datasets/downloads/`, obtain the files below, and verify
SHA256 before evaluating. Network is needed only for the initial public data
and tokenizer/dependency downloads. Offline evaluations need no model service.

| Input | Source | SHA256 | Bytes |
|---|---|---|---|
| `locomo10.json` | [Pinned LoCoMo commit](https://raw.githubusercontent.com/snap-research/locomo/3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376/data/locomo10.json) | `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` | 2805274 |
| `longmemeval_oracle.json` | [Official cleaned oracle](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json) | `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c` | 15388478 |

LongMemEval URL used `main` on2026-10-09; the content hash above identifies the
actual evaluated version. If the upstream file changes, record a new hash and
label the dataset version rather than comparing it as the same input. It is
cleaned **oracle**, not S/M or V2. See [official LongMemEval documentation](https://github.com/xiaowu0162/LongMemEval)
for the official judge and respective dataset terms.

LoCoMo is licensed **CC BY-NC4.0**; see [upstream terms](https://github.com/snap-research/locomo).
The harness measures text-based retrieval only and does not process image
content. Answer annotations, summaries, observations and `has_answer` flags
are excluded from persisted context. Reports identify retrieval-only metrics;
no model answer generation or official accuracy judge is inferred.

Download using ordinary HTTPS tools, for example:

```bash
mkdir -p benchmarks/datasets/downloads
curl --fail --location 'https://raw.githubusercontent.com/snap-research/locomo/3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376/data/locomo10.json' \
  --output benchmarks/datasets/downloads/locomo10.json
curl --fail --location 'https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json' \
  --output benchmarks/datasets/downloads/longmemeval_oracle.json
shasum -a256 benchmarks/datasets/downloads/*.json
```
