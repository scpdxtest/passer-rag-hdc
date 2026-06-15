**TABLE III.** RESULTS — HIERARCHY-AWARE SYNOPSIS SPAN EXPERIMENT, PYTHON TUTORIAL, N = 25 QUESTIONS, MISTRAL 7B READER, 3 JUDGES SPANNING SIZE CLASSES.

| Metric | Subset (n) | N (naïve) | H (hierarchy-aware) | p (McNemar / Wilcoxon) |
|---|---|:-:|:-:|:-:|
| Recall@10 | Summarisation (15) | 100.0% [79.6, 100.0] | 100.0% [79.6, 100.0] | 1.000 |
| Recall@10 | Control (10) | 30.0% [10.8, 60.3] | 100.0% [72.2, 100.0] | 0.016 |
| Synopsis-Recall@10 | Summarisation (15) | 86.7% [62.1, 96.3] | 100.0% [79.6, 100.0] | 0.500 |
| Hedging Rate | Summarisation (15) | 6.7% [1.2, 29.8] | 26.7% [10.9, 52.0] | 0.375 |
| Hedging Rate | Control (10) | 0.0% [0.0, 27.8] | 0.0% [0.0, 27.8] | 1.000 |
| Faithfulness — Mistral 7B (mean ± 95 % CI) | Summarisation (15) | 0.86 [0.75, 0.95] | 0.87 [0.74, 0.98] | 0.441 |
| Faithfulness — Llama 3.1 70B (mean ± 95 % CI) | Summarisation (15) | 0.46 [0.36, 0.56] | 0.48 [0.36, 0.61] | 1.000 |
| Faithfulness — Gemma 4 31B (mean ± 95 % CI) | Summarisation (15) | 0.39 [0.29, 0.49] | 0.41 [0.28, 0.54] | 0.985 |

Inter-judge agreement (Faithfulness, Summarisation subset, Pearson r): Mistral 7B vs Llama 3.1 70B: r=0.71; Mistral 7B vs Gemma 4 31B: r=0.60; Llama 3.1 70B vs Gemma 4 31B: r=0.90 (n=30)

Synopsis input size (mean chars), N = 905 (mean, n=16); H = 10398 (mean, n=16).
