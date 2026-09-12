# Following the prompts and reproducing the results

I used these prompts in this order:

| Step | Prompt | Purpose |
| --- | --- | --- |
| 1 | [extract_rules_codex.txt](extract_rules_codex.txt) | Use Codex to extract contract services and billing rules into JSON. |
| 2 | [service_matching_claude.txt](service_matching_claude.txt) | Use Claude to match invoice descriptions to the contract services. |
| 3 | [audit_invoices_codex.txt](audit_invoices_codex.txt) | Use Codex to implement and check the Python audit. |

## Reproduce the same submission

The code and saved Claude matches are already included. You do not need to send the prompts again or use an API key. Keep the original files in `contracts/`, `invoices/` and `claude matching service/` unchanged.

With Python 3.11 installed, open PowerShell in the repository root and run:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -B src/extract_rules.py --hospital 4
.venv/Scripts/python -B src/extract_rules.py --hospital 5
.venv/Scripts/python -B src/audit_invoices.py --submission
```

This extracts the rules, runs the audits for Hospitals 4 and 5, and combines their predictions into `outputs/submission.csv`. The result should contain **1,885 rows, with 139 flagged invoices**. Existing generated files are overwritten.


## Reproduce Hospital 1 predictions

After setup, run:

```powershell
.venv/Scripts/python -B src/extract_rules.py --hospital 1
.venv/Scripts/python -B src/audit_invoices.py --hospital 1
```

This writes `outputs/hospital_1_prediction.csv` with **913 rows and 58 flagged invoices**. Hospital 1 is used for development and is not included in the final submission. The results are explained in the [evaluation report](../Hospital1_Evaluation_Report.md).

## If you reuse the prompts

Replace `HOSPITAL` with `1`, `4` or `5` and supply the files listed in each prompt. The matching prompt returns JSON, which must be converted to the matched CSV format before auditing; that conversion is not automated here.

Rerunning Claude may produce different matches. Use the included matched CSVs to reproduce the saved result.
