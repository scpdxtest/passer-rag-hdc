**TABLE III.** RESULTS — HIERARCHY-AWARE SYNOPSIS SPAN EXPERIMENT, PYTHON TUTORIAL, N = 25 QUESTIONS, MISTRAL 7B READER + JUDGE.

| Metric | Subset (n) | N (naïve) | H (hierarchy-aware) | p (McNemar / Wilcoxon) |
|---|---|:-:|:-:|:-:|
| Recall@10 | Summarisation (15) | 100.0% [79.6, 100.0] | 100.0% [79.6, 100.0] | 1.000 |
| Recall@10 | Control (10) | 30.0% [10.8, 60.3] | 100.0% [72.2, 100.0] | 0.016 |
| Synopsis-Recall@10 | Summarisation (15) | 86.7% [62.1, 96.3] | 100.0% [79.6, 100.0] | 0.500 |
| Hedging Rate | Summarisation (15) | 6.7% [1.2, 29.8] | 26.7% [10.9, 52.0] | 0.375 |
| Hedging Rate | Control (10) | 0.0% [0.0, 27.8] | 0.0% [0.0, 27.8] | 1.000 |
| Faithfulness (mean ± 95 % CI) | Summarisation (15) | 0.86 [0.75, 0.95] | 0.87 [0.74, 0.98] | 0.441 |
| Synopsis input size (mean chars) | — | 905 (mean, n=16) | 10398 (mean, n=16) | n/a |
