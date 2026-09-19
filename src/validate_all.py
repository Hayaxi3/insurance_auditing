"""Validate generated matches and predictions for all five hospitals."""

from pathlib import Path
import json

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def load_csv(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def validate_hospital(hospital):
    source = load_csv(ROOT / f"invoices/hospital_{hospital}_line_items.csv")
    matched = load_csv(
        ROOT / f"service matches/hospital_{hospital}_line_items_matched.csv"
    )
    invoices = load_csv(ROOT / f"invoices/hospital_{hospital}_invoices.csv")
    prediction = load_csv(ROOT / f"outputs/hospital_{hospital}_prediction.csv")

    if not matched[list(source.columns)].equals(source):
        raise ValueError(f"Hospital {hospital}: matched rows differ from source lines")
    if not matched["line_id"].is_unique or len(matched) != len(source):
        raise ValueError(f"Hospital {hospital}: invalid matched-line coverage")

    invoice_ids = list(dict.fromkeys(invoices["invoice_id"]))
    if prediction["invoice_id"].tolist() != invoice_ids or not prediction["invoice_id"].is_unique:
        raise ValueError(f"Hospital {hospital}: invalid prediction invoice coverage/order")

    billed = invoices.groupby("invoice_id", sort=False)["invoice_total_cents"].apply(
        lambda values: sum(map(int, values))
    ).to_dict()
    for row in prediction.itertuples(index=False):
        categories = json.loads(row.error_category)
        if not isinstance(categories, list) or any(not isinstance(value, str) for value in categories):
            raise ValueError(f"Hospital {hospital}: invalid error categories")
        if (row.flagged == "1") != bool(categories):
            raise ValueError(f"Hospital {hospital}: flag/category disagreement")
        if int(row.billed_total_cents) != billed[row.invoice_id]:
            raise ValueError(f"Hospital {hospital}: billed total does not reconcile")
        if row.expected_total_cents and not row.expected_total_cents.lstrip("-").isdigit():
            raise ValueError(f"Hospital {hospital}: noninteger expected total")
        if not 0 <= float(row.confidence) <= 1:
            raise ValueError(f"Hospital {hospital}: invalid confidence")

    return {
        "lines": len(source),
        "invoices": len(prediction),
        "flagged": int((prediction["flagged"] == "1").sum()),
        "unknown": int((matched["service_status"] == "UNKNOWN").sum()),
        "unsure": int((matched["service_status"] == "UNSURE").sum()),
    }


def main():
    results = {}
    for hospital in range(1, 6):
        results[hospital] = validate_hospital(hospital)
        values = results[hospital]
        print(f"H{hospital}: lines={values['lines']}, invoices={values['invoices']}, "
              f"flagged={values['flagged']}, UNKNOWN={values['unknown']}, "
              f"UNSURE={values['unsure']}, integrity=PASS")

    submission = load_csv(ROOT / "outputs/submission.csv")
    expected = pd.concat([
        load_csv(ROOT / f"outputs/hospital_{hospital}_prediction.csv")
        for hospital in range(2, 6)
    ], ignore_index=True)
    if not submission.equals(expected):
        raise ValueError("Submission is not the exact H2-H5 prediction concatenation")
    print(f"Submission: rows={len(submission)}, "
          f"flagged={int((submission['flagged'] == '1').sum())}, integrity=PASS")

if __name__ == "__main__":
    main()
