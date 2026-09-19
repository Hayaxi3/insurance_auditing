# Hospital 1 Development Set Evaluation

## 1. Evaluation Results

The auditing pipeline was evaluated on the labeled Hospital 1 development set. First, the services and rules specified in the contract were extracted and organized into structured JSON. Invoice service descriptions were then mapped by the local deterministic matcher in `src/match_services.py`. It normalizes abbreviations, scores contract-service terminology, uses billed units as secondary evidence and uses exact documented prices only to break unresolved textual ties. After service mapping, deterministic Python rules were applied to validate billing amounts and contract compliance.

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
| unknown\_service                       |      12 | 11 |  0 |  1 |    100.0% |  91.7% |  95.7% |
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

The audit achieved perfect precision and recall in 15 of 18 error categories. It identified all 58 erroneous invoices without flagging any correct invoices, resulting in 100% invoice-level precision, recall, F1, and accuracy. The complete predicted category set matched the labels for 912 of 913 invoices.

## 2. Error Analysis

### 1. Unsupported Semantic Inference

Incomplete service descriptions can lead to unsupported service assignments. The audit then checks the assigned service's rate and unit basis, potentially reporting the wrong error categories.

**Example:** In `INV-H1-000236`, line `H1-L00236-03`, `Fract Outpatient Radiotherapy` was mapped to `Outpatient Metabolic Radiotherapy Fraction`, although `Metabolic` was missing and the price and unit did not match. This led the audit to report `unit_price_mismatch` and `wrong_unit_basis`, while the invoice label instead includes `unknown_service`.

## 3. Summary

The audit achieved perfect precision and recall in 15 of 18 error categories. The only invoice-level category mismatch occurred because an incomplete description was matched to a specific contracted service even though its distinguishing specialty was absent. The matcher can reject descriptions as `UNKNOWN` or mark ambiguous descriptions as `UNSURE`. It never selects a service simply because its price is closest. In this case, however, the available words were sufficient for the matcher to select a service even though the specialty was missing.

Python performs service matching, contract calculations, and invoice auditing. Hospital 1 labels are accessed only after prediction for evaluation and are never used to generate service matches or predictions. Codex assisted with writing and reviewing the implementation.
