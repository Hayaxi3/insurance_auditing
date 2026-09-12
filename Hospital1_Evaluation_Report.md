# Hospital 1 Development Set Evaluation

## 1. Evaluation Results

The auditing pipeline was evaluated on the labeled Hospital 1 development set. First, the services and rules specified in the contract were extracted and organized into structured JSON. Invoice service descriptions were then mapped to the corresponding contracted services using Claude to interpret abbreviated and varied service names, with billed prices used as additional evidence when descriptions were ambiguous. After service mapping, deterministic Python rules were applied to validate billing amounts and contract compliance.

Performance was measured per error category using precision, recall, and F1-score across 913 distinct invoice IDs. Each category is counted once per invoice.

| Error Category                         | Support | TP | FP | FN | Precision | Recall |     F1 |
| -------------------------------------- | ------: | -: | -: | -: | --------: | -----: | -----: |
| cross\_invoice\_duplicate              |       4 |  4 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| unit\_price\_mismatch                  |      10 | 10 |  1 |  0 |     90.9% | 100.0% |  95.2% |
| wrong\_unit\_basis                     |      11 | 11 |  1 |  0 |     91.7% | 100.0% |  95.7% |
| line\_total\_arithmetic                |       6 |  6 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| daily\_cap\_exceeded                   |       4 |  4 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| service\_date\_after\_invoice\_date    |       5 |  5 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| service\_date\_out\_of\_window         |       5 |  5 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| unknown\_service                       |      12 | 10 |  0 |  2 |    100.0% |  83.3% |  90.9% |
| volume\_discount\_omitted              |       4 |  4 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| invoice\_total\_mismatch               |       6 |  6 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| exclusion\_window\_violation           |       4 |  4 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| bundle\_not\_applied                   |       5 |  5 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| contract\_number\_mismatch             |       5 |  5 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| premium\_omitted                       |       3 |  3 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| volume\_discount\_incorrectly\_applied |       4 |  4 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| malformed\_service\_date               |       6 |  6 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| premium\_incorrectly\_applied          |       6 |  6 |  0 |  0 |    100.0% | 100.0% | 100.0% |
| duplicate\_invoice\_id                 |       5 |  5 |  0 |  0 |    100.0% | 100.0% | 100.0% |

The system achieved perfect precision and recall in 15 of the 18 evaluated categories. The predicted error categories exactly matched the labels for 911 of 913 invoices. Two invoices still have category errors. The analysis below explains the two remaining failure types.

## 2. Error Analysis

### 1. Unsupported Semantic Inference

Incomplete service descriptions can lead to unsupported service assignments. The audit then checks the assigned service's rate and unit basis, potentially reporting the wrong error categories.

**Example:** In `INV-H1-000236`, line `H1-L00236-03`, `Fract Outpatient Radiotherapy` was mapped to `Outpatient Metabolic Radiotherapy Fraction`, although “Metabolic” was missing and the price and unit did not match. This led the audit to report `unit_price_mismatch` and `wrong_unit_basis`, while the invoice label instead includes `unknown_service`.

### 2. UNKNOWN vs. UNSURE Classification

The system sometimes had difficulty deciding whether a service was unclear (`UNSURE`) or clearly not included in the contract (`UNKNOWN`).

**Example:** In `INV-H1-000811`, `Asst Specimen Anly` was classified as `UNSURE` because there was not enough information to choose between two possible services. The audit therefore did not report it as an `unknown_service`, although the invoice label includes it. This shows how leaving a match unresolved can miss an error.


## 3. Summary

The audit achieved perfect precision and recall in 15 of 18 categories on this development set. The remaining errors involve service assignments and distinguishing `UNKNOWN` from `UNSURE`. 

Claude was used for service matching, while Python handled the contract calculations and validation rules. Codex was used as a development assistant during implementation and debugging.
