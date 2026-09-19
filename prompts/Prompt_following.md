# Following the prompts and reproducing the results

I used these prompts in this order:

| Step | Prompt | Purpose |
| --- | --- | --- |
| 1 | [extract_rules_codex.txt](extract_rules_codex.txt) | Use Codex to write the python code to extract contract services and billing rules into JSON. |
| 2 | [match_services_codex.txt](match_services_codex.txt) | Use Codex to implement and verify deterministic local service matching. |
| 3 | [audit_invoices_codex.txt](audit_invoices_codex.txt) | Use Codex to implement and check the Python audit. |

## Reproduce the same submission

The runtime regenerates service matches locally. You do not need to send the prompts again or use an API key. Keep the original files in `contracts/` and `invoices/` unchanged.

With Python 3.11 installed, open PowerShell in the repository root and run:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -B src/run_all.py
```

This regenerates rules, service matches and predictions for Hospitals 1-5, then combines Hospitals 2-5 into `outputs/submission.csv`. The result contain **3,942 rows, with 285 flagged invoices**. Existing generated files are overwritten.

The same workflow can be inspected as three explicit stages:

```powershell
.venv/Scripts/python -B src/extract_rules.py --all
.venv/Scripts/python -B src/match_services.py --all
.venv/Scripts/python -B src/audit_invoices.py --all

 # Optional but recommended integrity validation
.venv/Scripts/python -B src/validate_all.py
```

Each stage also accepts `--hospital 1`, `--hospital 2`, `--hospital 3`, `--hospital 4` or `--hospital 5` instead of `--all`.


## If you reuse the prompts

Replace `HOSPITAL` with a number from 1 to 5 when using a prompt for one hospital, or use `--all` to process every hospital.

These prompts document how Codex assisted with development only. The finished pipeline runs locally using deterministic Python and does not send contracts, invoices, or patient data to Codex or any other model.
