# Invoice auditing

This solution identifies billing errors by comparing hospital invoices with their contracts. Python extracts the contract rules, locally matches invoice descriptions to contracted services for Hospitals 1-5, and then checks prices, quantities, dates, discounts, duplicate billing and invoice calculations.

The combined submission covers **Hospitals 2-5: 3,942 invoice IDs, 285 flagged**. Hospital 1 is used for labeled development and evaluation. No accuracy is claimed for Hospitals 2-5 because their labels are unavailable.

## Reproduce the submission

Use **Python 3.11** (tested with 3.11.2). Clone this repository and open a terminal in its root directory. Runtime dependencies, including pandas dependencies, are pinned in `requirements.txt`.

In PowerShell:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -B src/run_all.py
```

Installation needs access to a Python package index. After installation, the complete pipeline runs locally: **no API key, model download or saved model output is needed**. The single command rebuilds rules, matches and predictions for Hospitals 1-5, combines Hospitals 2-5 in `outputs/submission.csv`, and validates everything.

The command rebuilds contract JSON and predictions, validates the submission, and writes **[`outputs/submission.csv`](outputs/submission.csv)**. Generated files are overwritten. Submission rows contain Hospitals 2, 3, 4 and 5 in that order, with one row per distinct invoice ID.

## Two ways to run

The recommended one-command method is:

```powershell
.venv/Scripts/python -B src/run_all.py
```

The equivalent method runs the three processing stages, followed by validation:

```powershell
.venv/Scripts/python -B src/extract_rules.py --all
.venv/Scripts/python -B src/match_services.py --all
.venv/Scripts/python -B src/audit_invoices.py --all
.venv/Scripts/python -B src/validate_all.py
```

To process one hospital, use `--hospital N` instead of `--all` in the first three commands, where `N` is 1 through 5. After processing Hospitals 2-5 individually, run `src/audit_invoices.py --build-submission` and then `src/validate_all.py`.

| Hospital | Contract services | Line items | Invoice predictions | Flagged | Hospital-specific handling |
| --- | ---: | ---: | ---: | ---: | --- |
| H2 | 76 | 14,360 | 1,125 | 76 | Extracts rules from prose clauses and rejects contradictory service descriptions as `UNKNOWN`. |
| H3 | 120 | 11,655 | 932 | 70 | Combines three contract documents, selects amended rates by service date, and enforces contract and new-service effective dates. |
| H4 | 98 | 10,560 | 835 | 63 | Applies its contract-specific bundles, thresholds, caps, discounts, exclusions, and date rules. |
| H5 | 84 | 13,221 | 1,050 | 76 | Applies facility and plan-tier rates; exact documented prices only break ties between textually plausible matches. |

Hospitals 2-5 have no supplied labels, so these are reproducible contract-based results rather than measured accuracy scores.


## Reproduce Hospital 1 from raw inputs

Hospital 1 has a fully deterministic matching pipeline. It does not require an API key or use labels during matching and prediction.

PowerShell:

```powershell
python -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.txt
python -B src/run_all.py --evaluate-h1
```

macOS/Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -B src/run_all.py --evaluate-h1
```

This regenerates contract rules and service matches before auditing. Expected results are 913 invoice IDs, 58 flagged, 100% flag accuracy and 912/913 exact error-category matches.

`run_all.py` also validates line coverage, invoice coverage, totals, flags, and the combined submission.


## Deliverables

| Requirement | File |
| --- | --- |
| Runnable repository | This README, `requirements.txt`, `src/`, `contracts/`, `invoices/`, `labels/` and `submission_template.csv` |
| Submission | [`outputs/submission.csv`](outputs/submission.csv) |
| Hospital 1 evaluation and analysis | [`Hospital1_Evaluation_Report.md`](Hospital1_Evaluation_Report.md) |
| Prompts | [`prompts/Prompt_following.md`](prompts/Prompt_following.md) |
| One-page decision log | [`DECISION_LOG.md`](DECISION_LOG.md) |


## How it works

1. **Extract rules.** `src/extract_rules.py` converts the five contracts into structured JSON containing services, rates, caps, premiums, bundles, discounts and exclusions. It handles Hospital 2's prose clauses, Hospital 3's amendment dates, and Hospital 5's facility and plan-tier multipliers.
2. **Match services.** `src/match_services.py` locally normalizes invoice descriptions, expands controlled abbreviations and scores contract-service candidates. Text is the primary evidence; units and exact documented prices only break plausible ties. Contradictory or insufficient descriptions can return `UNKNOWN` or `UNSURE`.
3. **Audit invoices.** `src/audit_invoices.py` applies each hospital's contract rules, checks all 18 error categories and calculates expected invoice totals when the necessary context is available. Hospital 1 labels are used only for optional evaluation after predictions are generated.
4. **Build the submission.** The audit writes one prediction file per hospital and combines Hospitals 2-5, in order, into `outputs/submission.csv`. Hospital 1 remains the labeled evaluation dataset.
5. **Validate results.** `src/validate_all.py` verifies source-line preservation, invoice coverage, flags, categories, totals, confidence values and the exact Hospital 2-5 submission composition. `src/run_all.py` executes all five steps automatically.


## Repository layout

```text
contracts/                 Original contracts
invoices/                  Original headers, lines and nested JSONL
labels/                    Hospital 1 development labels
service matches/           Deterministic matches generated by the pipeline
extracted contract rule/   Contract JSON; regenerated by the pipeline
prompts/                   Saved prompts 
src/                       Extraction, matching, auditing, validation and runner scripts
outputs/                   Submission and hospital predictions
```


## AI Assistance

Codex assisted with writing and reviewing the extraction, matching, and auditing code. The development prompts and reproduction steps are documented in [`prompts/Prompt_following.md`](prompts/Prompt_following.md).
