from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import argparse
import csv
import json
import pandas as pd
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


ERROR_CATEGORIES = [
    "cross_invoice_duplicate", "unit_price_mismatch", "wrong_unit_basis",
    "line_total_arithmetic", "daily_cap_exceeded", "service_date_after_invoice_date",
    "service_date_out_of_window", "unknown_service", "volume_discount_omitted",
    "invoice_total_mismatch", "exclusion_window_violation", "bundle_not_applied",
    "contract_number_mismatch", "premium_omitted", "volume_discount_incorrectly_applied",
    "malformed_service_date", "premium_incorrectly_applied", "duplicate_invoice_id",
]


KNOWN_STATUSES = {"MATCHED", "MATCHED_BY_PRICE"}


UNIT_BASES_1 = {"per day of service": "per_day", "per night of occupancy": "per_night",
                "per item supplied": "per_item", "per hour, per item": "per_hour_per_item"}


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def integer(value, field):
    if not re.fullmatch(r"-?\d+", str(value)):
        raise ValueError(f"{field} must be an integer, got {value!r}")
    return int(value)


def adjust(cents, percent):
    """Apply one signed percentage adjustment, rounding half away from zero."""
    return int((Decimal(cents) * (Decimal(100) + Decimal(str(percent))) / 100)
               .quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def price(base, premiums=(), discount=0):
    # H1 facility and plan multipliers are both exactly one (clauses 1.2/1.3).
    for premium in premiums:
        base = adjust(base, premium)
    return adjust(base, -discount)


def add_error(row, category):
    if category not in row["errors"]:
        row["errors"].append(category)


def ordered(errors):
    return [category for category in ERROR_CATEGORIES if category in errors]


def reported_errors(errors):
    """Report specific causes per line while retaining internal review notes.

    Suppression is per LINE, so a separate unexplained rate error on another
    line remains an invoice-level unit_price_mismatch.
    """
    result = set(errors)
    if result & {"bundle_not_applied", "premium_omitted", "premium_incorrectly_applied",
                 "volume_discount_omitted", "volume_discount_incorrectly_applied"}:
        result.discard("unit_price_mismatch")
    if "service_date_out_of_window" in result:
        result.discard("service_date_after_invoice_date")
    return ordered(result)


def csv_write(rows, path):
    # Object dtype prevents nullable integer cents from being coerced to float.
    pd.DataFrame(rows, dtype=object).to_csv(path, index=False, lineterminator="\n")


def submission_confidence(items, errors, expected, header):
    """User-defined evidence tiers, not calibrated probabilities. Labels excluded.

    Incompleteness/inference takes precedence over deterministic error evidence.
    """
    if any(r["service_status"] == "UNSURE" or
           (r["service_status"] in KNOWN_STATUSES and not r["rule"]) for r in items):
        return "0.60", "Incomplete: UNSURE mapping or unavailable selected-service rules"
    scores = [Decimal(r.get("confidence") or "0") for r in items]
    if any(not score.is_finite() or not 0 <= score <= 1 for score in scores):
        raise ValueError("Invalid input matching confidence")
    reasons = []
    if expected is None or not header or header["conflicting_fields"]:
        reasons.append("incomplete total or invoice context")
    if any(r["service_status"] == "MATCHED_BY_PRICE" for r in items):
        reasons.append("price-based service inference")
    if scores and min(scores) < Decimal("0.90"):
        reasons.append("source matching confidence below 0.90")
    if any({"unit_price_mismatch", "wrong_unit_basis"} <= set(r["errors"]) for r in items):
        reasons.append("both price and unit basis conflict with the supplied service")
    if reasons:
        return "0.80", "; ".join(reasons)
    deterministic = {"line_total_arithmetic", "invoice_total_mismatch", "malformed_service_date",
                     "service_date_after_invoice_date", "service_date_out_of_window",
                     "contract_number_mismatch", "duplicate_invoice_id"}
    if errors and set(errors) <= deterministic:
        return "0.99", "Deterministic error evidence with no identified completeness or inference limitation"
    return "0.90", "Strong supplied service and contract-rule evidence"


def audit_hospital_1(lines, invoices, contract, line_context=None):
    """Pure audit function. Inputs are DataFrames and a parsed contract dictionary."""
    meta = contract["contract_metadata"]
    if meta["rounding_convention"] != "half_up_cent":
        raise ValueError("Unsupported rounding convention")
    first = datetime.strptime(meta["effective_from"], "%d %B %Y").date()
    last = datetime.strptime(meta["effective_to"], "%d %B %Y").date()
    services = {s["service_name"]: s for s in contract["services"]}
    if len(services) != len(contract["services"]) or any(type(s["rate_cents"]) is not int for s in services.values()):
        raise ValueError("Duplicate services or noninteger contract base rates")
    if lines["line_id"].duplicated().any():
        raise ValueError("line_id must be unique; repeated billing events with different IDs are retained")

    # Collapse headers to a unique join key without selecting conflicting values.
    headers, header_rows = {}, []
    for invoice_id, group in invoices.groupby("invoice_id", sort=False):
        records = group.to_dict("records")
        header = {"invoice_id": invoice_id}
        for field in invoices.columns:
            values = {r[field] for r in records}
            header[field] = next(iter(values)) if len(values) == 1 else None
        header["header_count"] = len(records)
        header["conflicting_fields"] = [f for f in invoices.columns if header[f] is None]
        header["submitted_totals"] = [integer(r["invoice_total_cents"], "invoice_total_cents") for r in records]
        header["contract_numbers"] = [r["contract_number"] for r in records]
        headers[invoice_id] = header
        header_rows.append(header)
    merged = lines.merge(pd.DataFrame(header_rows, dtype=object), on="invoice_id", how="left",
                         validate="many_to_one", sort=False, indicator=True)
    if len(merged) != len(lines):
        raise AssertionError("Join changed the number of invoice lines")
    rows = []
    for record in merged.to_dict("records"):
        joined = record.pop("_merge") == "both"
        if not joined:
            for field in invoices.columns:
                if field != "invoice_id":
                    record[field] = None
        if line_context is not None:
            context = line_context[record["line_id"]]
            for field in invoices.columns:
                if field != "invoice_id":
                    record[field] = context[field]
        record.update(errors=[], notes=[], joined=joined, rule=None)
        for field in ("quantity", "unit_price_cents", "line_total_cents"):
            record[field] = integer(record[field], field)
        if record["quantity"] < 0:
            raise ValueError("Negative quantities require credit-note semantics not specified by H1")
        record["day"] = parse_date(record["service_date"])
        record["invoice_day"] = parse_date(record.get("invoice_date"))
        record["in_term"] = record["day"] is not None and first <= record["day"] <= last
        if record["service_status"] in KNOWN_STATUSES:
            record["rule"] = services.get(record["matched_service"])
            if record["rule"] is None:
                record["notes"].append("Selected service missing from contract; service checks skipped")
        elif record["service_status"] not in {"UNKNOWN", "UNSURE"}:
            raise ValueError(f"Unsupported service status: {record['service_status']}")
        if not joined:
            record["notes"].append("Missing invoice metadata join")
        elif headers[record["invoice_id"]]["conflicting_fields"]:
            record["notes"].append("Conflicting invoice metadata: " + ", ".join(headers[record["invoice_id"]]["conflicting_fields"]))
        if record["service_status"] == "UNSURE":
            record["notes"].append("Uncertain service: service-specific checks unavailable")
        record.update(expected_unit_price_cents=None, expected_line_total_cents=None,
                      billable_quantity=None, daily_quantity=None, cumulative_prior_quantity=None,
                      applied_discount_percent=0, applied_premiums=[], bundle_applies=False,
                      exclusion_applies=False, duplicate_event=False, price_ready=False,
                      later_duplicate=False,
                      price_alternatives={}, pricing_missing_reasons=[])
        rows.append(record)

    daily = defaultdict(list)
    patient_dates = defaultdict(list)
    by_invoice = defaultdict(list)
    for i, row in enumerate(rows):
        by_invoice[row["invoice_id"]].append(i)
        if row["rule"] and row["day"] and row.get("patient_id"):
            daily[(row["patient_id"], row["matched_service"], row["day"])].append(i)
            patient_dates[(row["patient_id"], row["matched_service"])].append(row["day"])
    for dates in patient_dates.values():
        dates.sort()
    for indexes in daily.values():
        total = sum(rows[i]["quantity"] for i in indexes)
        for i in indexes:
            rows[i]["daily_quantity"] = total

    # H1 2.4/7.1 counts identified billed units across the contract, including duplicates.
    running = Counter()
    for row in sorted((r for r in rows if r["rule"] and r["in_term"]),
                      key=lambda r: (r["day"], r["line_id"])):
        name = row["matched_service"]
        row["cumulative_prior_quantity"] = running[name]
        running[name] += row["quantity"]
    # A known volume service with an undatable line has uncertain placement.
    undated_services = {r["matched_service"] for r in rows if r["rule"] and not r["day"]}
    unknown_service_rows = [r for r in rows if r["service_status"] == "UNSURE" or
                            (r["service_status"] in KNOWN_STATUSES and not r["rule"])]
    unknown_context = [r for r in rows if r["rule"] and (not r["day"] or not r.get("patient_id"))]

    # Prepare contract rate components once; category checks below reuse them.
    for row in rows:
        rule = row["rule"]
        if not rule:
            continue
        missing = row["pricing_missing_reasons"]
        if row["day"] is not None and not row["in_term"]:
            missing.append("No contractual service date available")
        elif row["day"] is None:
            row["notes"].append("Provisional pricing despite malformed service date: contract-term eligibility unverified")
            if any(rule.get(k) is not None for k in
                   ("threshold_quantity", "bundle_with", "volume_threshold_1",
                    "daily_cap", "exclusion_with", "weekend_uplift_percent")):
                missing.append("Malformed date prevents determining date-dependent adjustments or billability")
        if not row["joined"]:
            missing.append("Missing invoice metadata")
        patient, day = row.get("patient_id"), row["day"]
        related = {row["matched_service"], rule.get("bundle_with"), rule.get("exclusion_with")}
        # Unidentified/contextless events may change daily, bundle or exclusion
        # eligibility. Unknown (uncontracted) services do not enter utilisation.
        possible_neighbors = [other for other in unknown_service_rows + unknown_context
                              if other is not row and
                              (not other.get("patient_id") or not patient or other["patient_id"] == patient) and
                              (not other["rule"] or other["matched_service"] in related) and
                              # H1 11.3 bounds a contract-valid service date by
                              # its invoice date, even when the line date is malformed.
                              # This excludes impossible overlap without inventing a date.
                              (other["day"] is not None or day is None or
                               other["invoice_day"] is None or
                               other["invoice_day"] >= day - timedelta(days=rule.get("exclusion_days") or 0)) and
                              (not other["day"] or not day or abs((other["day"] - day).days) <= (rule.get("exclusion_days") or 0))]
        contextual = rule.get("threshold_quantity") is not None or rule.get("bundle_with") is not None
        pricing_neighbors = [other for other in possible_neighbors if not other["rule"] or
                             (rule.get("threshold_quantity") is not None and other["matched_service"] == row["matched_service"] and
                              not (row["daily_quantity"] is not None and row["daily_quantity"] > rule["threshold_quantity"])) or
                             (other["matched_service"] == rule.get("bundle_with") and
                              (patient, rule.get("bundle_with"), day) not in daily)]
        if contextual and (not patient or pricing_neighbors):
            missing.append("Incomplete patient/day context for premiums or bundles")
        # Only an unknown triggering service can change an exclusion; an
        # undated occurrence of the excluded service itself cannot do so.
        billable_neighbors = [other for other in possible_neighbors
                              if not other["rule"] or
                              (rule.get("daily_cap") is not None and other["matched_service"] == row["matched_service"]) or
                              other["matched_service"] == rule.get("exclusion_with")]
        row["uncertain_billable_context"] = bool(billable_neighbors or not patient)
        row["bundle_applies"] = bool(patient and day and rule.get("bundle_with") and
                                        (patient, rule["bundle_with"], day) in daily)
        base = rule["bundled_rate_cents"] if row["bundle_applies"] else rule["rate_cents"]
        premiums = []
        if rule.get("threshold_quantity") is not None and row["daily_quantity"] is not None and row["daily_quantity"] > rule["threshold_quantity"]:
            premiums.append(rule["threshold_uplift_percent"])
        if rule.get("weekend_uplift_percent") is not None and day and day.weekday() >= 5:
            premiums.append(rule["weekend_uplift_percent"])
        prior = row["cumulative_prior_quantity"]
        discount = max([rule[f"volume_discount_{tier}_percent"] for tier in (1, 2)
                        if rule.get(f"volume_threshold_{tier}") is not None and prior is not None
                        and prior > rule[f"volume_threshold_{tier}"]] or [0])
        if rule.get("volume_threshold_1") is not None:
            if row["matched_service"] in undated_services or any(
                    not other["day"] or (other["in_term"] and day and
                    (other["day"], other["line_id"]) < (day, row["line_id"])) for other in unknown_service_rows):
                # The deepest tier remains certain if already reached even
                # without the unidentified units; otherwise abstain.
                deepest = max(rule.get("volume_discount_1_percent") or 0, rule.get("volume_discount_2_percent") or 0)
                if discount < deepest:
                    missing.append("Uncertain prior utilisation")
        row["applied_premiums"] = premiums
        row["applied_discount_percent"] = discount
        if missing:
            row["notes"].extend(missing)
            continue
        row["price_ready"] = True
        expected = price(base, premiums, discount)
        row["expected_unit_price_cents"] = expected
        service_discounts = {rule[f"volume_discount_{tier}_percent"] for tier in (1, 2)
                             if rule.get(f"volume_discount_{tier}_percent") is not None}
        # Evidence must identify an actual premium of THIS service whose
        # condition is false. Arbitrary rate increases remain generic mismatches.
        inactive_premiums = set()
        if rule.get("threshold_quantity") is not None and row["daily_quantity"] <= rule["threshold_quantity"]:
            inactive_premiums.add(rule["threshold_uplift_percent"])
        if rule.get("weekend_uplift_percent") is not None and day.weekday() < 5:
            inactive_premiums.add(rule["weekend_uplift_percent"])
        row["price_alternatives"] = {
            "without_discount": price(base, premiums),
            "without_bundle": price(rule["rate_cents"], premiums, discount),
            "omitted_premium": {price(base, premiums[:i] + premiums[i + 1:], discount) for i in range(len(premiums))},
            "wrong_discount": {price(base, premiums, d) for d in service_discounts if d != discount},
            "extra_premium": {price(base, premiums + [p], discount) for p in inactive_premiums},
        }

    # ============================================================
    # 1. cross_invoice_duplicate
    # ============================================================
    for indexes in daily.values():
        if len({rows[i]["invoice_id"] for i in indexes}) > 1:
            # Attribute rebilling to invoices issued strictly AFTER the earliest
            # dated invoice. With missing/tied dates, don't arbitrarily blame an
            # original: retain review notes and unavailable totals instead.
            days = [rows[i]["invoice_day"] for i in indexes]
            earliest = min(days) if all(days) else None
            earliest_ids = {rows[i]["invoice_id"] for i in indexes if rows[i]["invoice_day"] == earliest}
            for i in indexes:
                later = earliest is not None and rows[i]["invoice_day"] > earliest
                if later:
                    add_error(rows[i], "cross_invoice_duplicate")
                    rows[i]["later_duplicate"] = True
                    rows[i]["notes"].append("Rebilling after an earlier invoice for this patient/service/day")
                elif earliest is None or len(earliest_ids) > 1:
                    rows[i]["duplicate_event"] = True
                    rows[i]["notes"].append("Repeated event with ambiguous invoice chronology; attribution needs review")
        elif len(indexes) > 1:
            # Keep within-invoice repeats as review notes: the permitted 18
            # categories contain no corresponding error. Payable allocation is unclear.
            for i in indexes:
                rows[i]["duplicate_event"] = True
                rows[i]["notes"].append("Repeated service/day on one invoice; payable allocation unavailable")

    # ============================================================
    # 2. unit_price_mismatch
    # ============================================================
    for row in rows:
        if row["price_ready"] and row["unit_price_cents"] != row["expected_unit_price_cents"]:
            add_error(row, "unit_price_mismatch")

    # ============================================================
    # 3. wrong_unit_basis
    # ============================================================
    for row in rows:
        if row["rule"]:
            basis = row["rule"]["unit_basis"]
            expected_basis = UNIT_BASES_1.get(basis, basis.replace(" ", "_"))
            if row["unit_basis_as_billed"].strip().lower().replace(" ", "_") != expected_basis:
                add_error(row, "wrong_unit_basis")

    # ============================================================
    # 4. line_total_arithmetic
    # ============================================================
    for row in rows:
        if row["unit_price_cents"] * row["quantity"] != row["line_total_cents"]:
            add_error(row, "line_total_arithmetic")

    # ============================================================
    # 5. daily_cap_exceeded
    # ============================================================
    for row in rows:
        if row["rule"] and row["rule"].get("daily_cap") is not None and row["daily_quantity"] is not None:
            if row["daily_quantity"] > row["rule"]["daily_cap"]:
                add_error(row, "daily_cap_exceeded")

    # ============================================================
    # 6. service_date_after_invoice_date
    # ============================================================
    for row in rows:
        if row["day"] and row["invoice_day"] and row["day"] > row["invoice_day"]:
            add_error(row, "service_date_after_invoice_date")

    # ============================================================
    # 7. service_date_out_of_window
    # ============================================================
    for row in rows:
        if row["day"] and not row["in_term"]:
            add_error(row, "service_date_out_of_window")

    # ============================================================
    # 8. unknown_service
    # ============================================================
    for row in rows:
        if row["service_status"] == "UNKNOWN":
            add_error(row, "unknown_service")

    # ============================================================
    # 9. volume_discount_omitted
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and row["applied_discount_percent"] and
                row["unit_price_cents"] == row["price_alternatives"]["without_discount"]):
            add_error(row, "volume_discount_omitted")

    # ============================================================
    # 10. invoice_total_mismatch
    # ============================================================
    invoice_errors = {invoice_id: [] for invoice_id in headers}
    billed_sums = {invoice_id: sum(rows[i]["line_total_cents"] for i in indexes)
                   for invoice_id, indexes in by_invoice.items()}
    for invoice_id, header in headers.items():
        # Compare arithmetic to submitted total, NOT expected contract total.
        # Other categories describe pricing differences. Conflicting duplicate
        # totals cannot be associated with their individual line sets by ID alone.
        if line_context is not None:
            record_groups = defaultdict(list)
            for i in by_invoice.get(invoice_id, []):
                record_groups[line_context[rows[i]["line_id"]]["source_record"]].append(rows[i])
            if any(sum(r["line_total_cents"] for r in group) != integer(group[0]["invoice_total_cents"], "invoice_total_cents")
                   for group in record_groups.values()):
                invoice_errors[invoice_id].append("invoice_total_mismatch")
        elif header["invoice_total_cents"] is not None:
            if integer(header["invoice_total_cents"], "invoice_total_cents") != billed_sums.get(invoice_id, 0):
                invoice_errors[invoice_id].append("invoice_total_mismatch")

    # ============================================================
    # 11. exclusion_window_violation
    # ============================================================
    for row in rows:
        rule = row["rule"]
        if rule and rule.get("exclusion_with") and row["day"] and row.get("patient_id"):
            dates = patient_dates[(row["patient_id"], rule["exclusion_with"])]
            lower = row["day"] - timedelta(days=rule["exclusion_days"])
            position = bisect_left(dates, lower)
            if position < len(dates) and dates[position] <= row["day"] + timedelta(days=rule["exclusion_days"]):
                row["exclusion_applies"] = True
                add_error(row, "exclusion_window_violation")

    # ============================================================
    # 12. bundle_not_applied
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and row["bundle_applies"] and
                row["unit_price_cents"] == row["price_alternatives"]["without_bundle"]):
            add_error(row, "bundle_not_applied")

    # ============================================================
    # 13. contract_number_mismatch
    # ============================================================
    for invoice_id, header in headers.items():
        if any(number != meta["contract_number"] for number in header["contract_numbers"]):
            invoice_errors[invoice_id].append("contract_number_mismatch")

    # ============================================================
    # 14. premium_omitted
    # ============================================================
    for row in rows:
        if "unit_price_mismatch" in row["errors"] and row["unit_price_cents"] in row["price_alternatives"]["omitted_premium"]:
            add_error(row, "premium_omitted")

    # ============================================================
    # 15. volume_discount_incorrectly_applied
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and
                "volume_discount_omitted" not in row["errors"] and
                row["unit_price_cents"] in row["price_alternatives"]["wrong_discount"]):
            add_error(row, "volume_discount_incorrectly_applied")

    # ============================================================
    # 16. malformed_service_date
    # ============================================================
    for row in rows:
        if row["day"] is None:
            add_error(row, "malformed_service_date")

    # ============================================================
    # 17. premium_incorrectly_applied
    # ============================================================
    for row in rows:
        if "unit_price_mismatch" in row["errors"] and row["unit_price_cents"] in row["price_alternatives"]["extra_premium"]:
            add_error(row, "premium_incorrectly_applied")

    # ============================================================
    # 18. duplicate_invoice_id
    # ============================================================
    for invoice_id, header in headers.items():
        if header["header_count"] > 1:
            invoice_errors[invoice_id].append("duplicate_invoice_id")

    # Expected payable totals: known exclusions, later duplicate billings and uncontracted services are
    # nonbillable. Never manufacture a payable amount for UNSURE, invalid dates,
    # missing context or duplicate events whose invoice chronology is ambiguous.
    for row in rows:
        rule = row["rule"]
        if row["service_status"] == "UNKNOWN" or row["exclusion_applies"] or row["later_duplicate"] or (row["day"] is not None and not row["in_term"]):
            row["billable_quantity"] = 0
            row["expected_line_total_cents"] = 0
        elif row["price_ready"] and not row["duplicate_event"]:
            if rule.get("daily_cap") is not None or rule.get("exclusion_with"):
                if row.get("uncertain_billable_context"):
                    row["notes"].append("Incomplete context for cap/exclusion payable total")
                    continue
            quantity = min(row["quantity"], rule["daily_cap"]) if rule.get("daily_cap") is not None else row["quantity"]
            row["billable_quantity"] = quantity
            row["expected_line_total_cents"] = row["expected_unit_price_cents"] * quantity
        row["errors"] = ordered(row["errors"])

    submission, invoice_debug, line_debug = [], [], []
    for invoice_id in dict.fromkeys([*headers, *by_invoice]):
        header = headers.get(invoice_id)
        items = [rows[i] for i in by_invoice.get(invoice_id, [])]
        errors = ordered(set(invoice_errors.get(invoice_id, [])) | {e for r in items for e in reported_errors(r["errors"])})
        expected = None
        if items and all(r["expected_line_total_cents"] is not None for r in items) and header and (line_context is not None or not header["conflicting_fields"]):
            expected = sum(r["expected_line_total_cents"] for r in items)
        confidence, confidence_reason = submission_confidence(items, errors, expected, header)
        if expected is not None and any(r["day"] is None for r in items):
            confidence, confidence_reason = "0.80", "Provisional total: malformed service date; term eligibility unverified"
        billed = integer(header["invoice_total_cents"], "invoice_total_cents") if header and header["invoice_total_cents"] is not None else None
        if line_context is not None and header and header["header_count"] > 1:
            billed = sum(header["submitted_totals"])
        result = dict(invoice_id=invoice_id, flagged=int(bool(errors)), error_category=json.dumps(errors),
                      expected_total_cents=expected, billed_total_cents=billed, confidence=str(confidence))
        submission.append(result)
        invoice_debug.append({**result, "confidence_reason": confidence_reason,
                              "sum_billed_line_totals_cents": billed_sums.get(invoice_id, 0),
                              "source_invoice_records": header["header_count"] if header else 0,
                              "submitted_invoice_totals_cents": json.dumps(header["submitted_totals"] if header else []),
                              "conflicting_metadata": json.dumps(header["conflicting_fields"] if header else []),
                              "has_unsure_service": any(r["service_status"] == "UNSURE" for r in items),
                              "exact_total_available": expected is not None and all(r["day"] is not None for r in items),
                              "provisional_date_total": expected is not None and any(r["day"] is None for r in items)})
    for row in rows:
        fields = ["line_id", "invoice_id", "line_no", "service_date", "description", "matched_service",
                  "service_status", "confidence", "quantity", "unit_basis_as_billed", "unit_price_cents", "line_total_cents",
                  "expected_unit_price_cents", "billable_quantity", "expected_line_total_cents", "daily_quantity",
                  "cumulative_prior_quantity", "applied_discount_percent", "bundle_applies", "exclusion_applies"]
        line_errors = ordered(row["errors"] + invoice_errors.get(row["invoice_id"], []))
        line_debug.append({**{f: row.get(f) for f in fields}, "patient_id": row.get("patient_id"),
                           "invoice_date": row.get("invoice_date"), "flagged": int(bool(line_errors)),
                           "contract_base_rate_cents": row["rule"]["rate_cents"] if row["rule"] else None,
                           "contract_unit_basis": row["rule"]["unit_basis"] if row["rule"] else None,
                           "applied_premiums": json.dumps(row["applied_premiums"]),
                           "error_category": json.dumps(line_errors),
                           "reported_error_category": json.dumps(reported_errors(line_errors)),
                           "review_notes": json.dumps(row["notes"])})
    stats = {"Total invoices": len(invoices), "Distinct invoice IDs": len(headers), "Total line items": len(rows),
             "MATCHED rows": sum(r["service_status"] in KNOWN_STATUSES for r in rows),
             "MATCHED_BY_PRICE rows (included above)": sum(r["service_status"] == "MATCHED_BY_PRICE" for r in rows),
             "UNKNOWN rows": sum(r["service_status"] == "UNKNOWN" for r in rows),
             "UNSURE rows": sum(r["service_status"] == "UNSURE" for r in rows),
             "Flagged invoices": sum(r["flagged"] for r in submission),
             "Unmatched contract-service lookups": sum(r["service_status"] in KNOWN_STATUSES and not r["rule"] for r in rows),
             "Missing invoice joins": sum(not r["joined"] for r in rows),
             "Invoices with UNSURE services and unavailable expected total": sum(r["has_unsure_service"] and not r["exact_total_available"] for r in invoice_debug),
             "Invoices with unavailable expected total (all causes)": sum(r["expected_total_cents"] is None for r in invoice_debug),
             "Invoices with provisional malformed-date totals": sum(r["provisional_date_total"] for r in invoice_debug)}
    return submission, line_debug, invoice_debug, stats


def evaluate(submission_path, labels_path, output_dir):
    """Read frozen predictions first; labels are accessible only in this function."""
    predictions = pd.read_csv(submission_path, dtype=str, keep_default_na=False)
    labels = pd.read_csv(labels_path, dtype=str, keep_default_na=False)
    # Repeated identifiers cannot be assigned to distinct header records from the
    # prediction key alone. Exclude them from evaluation, rather than multiply rows.
    labels = labels[~labels["invoice_id"].duplicated(keep=False)]
    compared = predictions.merge(labels, on="invoice_id", validate="one_to_one", suffixes=("", "_label"))
    truth_sets = [{c for c in x.split("|") if c}
                  for x in compared["error_categories"]]
    predicted_sets = [set(json.loads(x)) for x in compared["error_category"]]
    disagreements = [{"invoice_id": invoice_id, "extra_categories": json.dumps(ordered(p - t)),
                      "missing_categories": json.dumps(ordered(t - p))}
                     for invoice_id, p, t in zip(compared["invoice_id"], predicted_sets, truth_sets) if p != t]
    pd.DataFrame(disagreements, columns=["invoice_id", "extra_categories", "missing_categories"]).to_csv(
        output_dir / "hospital_1_category_disagreements.csv", index=False, lineterminator="\n")
    metrics = []
    for category in ERROR_CATEGORIES:
        tp = sum(category in p and category in t for p, t in zip(predicted_sets, truth_sets))
        fp = sum(category in p and category not in t for p, t in zip(predicted_sets, truth_sets))
        fn = sum(category not in p and category in t for p, t in zip(predicted_sets, truth_sets))
        metrics.append(dict(error_category=category, support=tp + fn, true_positive=tp, false_positive=fp, false_negative=fn,
                            precision=tp / (tp + fp) if tp + fp else None,
                            recall=tp / (tp + fn) if tp + fn else None,
                            f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None))
    csv_write(metrics, output_dir / "hospital_1_evaluation.csv")
    true_flags = compared["is_erroneous"].str.lower().isin(["1", "true"])
    accuracy = (compared["flagged"].eq("1") == true_flags).mean()
    summary = {"evaluated_invoice_ids": len(compared),
               "excluded_prediction_ids": len(predictions) - len(compared),
               "flag_true_positive": int((compared["flagged"].eq("1") & true_flags).sum()),
               "flag_false_positive": int((compared["flagged"].eq("1") & ~true_flags).sum()),
               "flag_false_negative": int((~compared["flagged"].eq("1") & true_flags).sum()),
               "exact_category_matches": len(compared) - len(disagreements),
               "flag_accuracy": float(accuracy)}
    (output_dir / "hospital_1_evaluation_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Evaluation after output: {len(compared)} unambiguous invoice IDs, flag accuracy {accuracy:.2%}")


def run_hospital_1():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluate", action="store_true", help="Evaluate only after predictions are saved")
    args = parser.parse_args()
    # Legacy H1 CSV has surplus empty cells; reject nonempty surplus data.
    with (ROOT / "service matches/hospital_1_line_items_matched.csv").open(encoding="utf-8-sig", newline="") as stream:
        records = list(csv.reader(stream))
    columns = records[0]
    if any(len(row) < len(columns) or any(row[len(columns):]) for row in records[1:]):
        raise ValueError("Malformed Hospital 1 matched CSV")
    lines = pd.DataFrame([row[:len(columns)] for row in records[1:]], columns=columns)
    invoices = pd.read_csv(ROOT / "invoices/hospital_1_invoices.csv", dtype=str, keep_default_na=False)
    contract = json.loads((ROOT / "extracted contract rule/hospital_1_services.json").read_text(encoding="utf-8"))
    originals = pd.read_csv(ROOT / "invoices/hospital_1_line_items.csv", dtype=str, keep_default_na=False)
    if not lines[list(originals.columns)].equals(originals):
        raise ValueError("Matched file differs from original invoice lines")
    contexts = {}
    source_headers = []
    for index, text in enumerate((ROOT / "invoices/hospital_1_invoices.jsonl").read_text(encoding="utf-8").splitlines()):
        source = json.loads(text)
        header = {k: str(source[k]) for k in invoices.columns}
        source_headers.append(header)
        for item in source["line_items"]:
            if item["line_id"] in contexts:
                raise ValueError("Duplicate line identifier in JSONL")
            contexts[item["line_id"]] = dict(header, source_record=index, original=item)
    if source_headers != invoices.to_dict("records"):
        raise ValueError("JSONL headers disagree with invoice CSV")
    if set(contexts) != set(lines["line_id"]):
        raise ValueError("JSONL line coverage disagrees with CSV")
    for row in originals.to_dict("records"):
        if any(str(contexts[row["line_id"]]["original"][k]) != v for k, v in row.items()):
            raise ValueError("JSONL line fields disagree with CSV")
    submission, line_debug, invoice_debug, stats = audit_hospital_1(lines, invoices, contract, contexts)
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    csv_write(submission, output / "hospital_1_prediction.csv")
    counts = Counter(e for row in submission for e in json.loads(row["error_category"]))
    print(f"Hospital 1: {len(submission)} invoices, {stats['Flagged invoices']} flagged. Saved outputs/hospital_1_prediction.csv")
    if args.evaluate:
        evaluate(output / "hospital_1_prediction.csv", ROOT / "labels/hospital_1_labels.csv", output)


def audit_hospital_2(lines, invoices, contract, line_context=None):
    """Audit H2 using its H1-compatible calculation and invoicing conventions."""
    if contract["contract_metadata"]["contract_number"] != "INS-H2-2024-1183":
        raise ValueError("Expected Hospital 2 contract")
    return audit_hospital_1(lines, invoices, contract, line_context)


def run_hospital_2():
    lines = pd.read_csv(ROOT / "service matches/hospital_2_line_items_matched.csv",
                        dtype=str, keep_default_na=False)
    invoices = pd.read_csv(ROOT / "invoices/hospital_2_invoices.csv", dtype=str,
                           keep_default_na=False)
    contract = json.loads((ROOT / "extracted contract rule/hospital_2_services.json")
                          .read_text(encoding="utf-8"))
    originals = pd.read_csv(ROOT / "invoices/hospital_2_line_items.csv", dtype=str,
                            keep_default_na=False)
    if not lines[list(originals.columns)].equals(originals):
        raise ValueError("Hospital 2 matched file differs from original invoice lines")

    contexts = {}
    source_headers = []
    jsonl = ROOT / "invoices/hospital_2_invoices.jsonl"
    for index, text in enumerate(jsonl.read_text(encoding="utf-8").splitlines()):
        source = json.loads(text)
        header = {key: str(source[key]) for key in invoices.columns}
        source_headers.append(header)
        for item in source["line_items"]:
            line_id = item["line_id"]
            if line_id in contexts:
                raise ValueError("Duplicate Hospital 2 line identifier in JSONL")
            contexts[line_id] = dict(header, source_record=index, original=item)
    if source_headers != invoices.to_dict("records"):
        raise ValueError("Hospital 2 JSONL headers disagree with invoice CSV")
    if set(contexts) != set(lines["line_id"]):
        raise ValueError("Hospital 2 JSONL line coverage disagrees with matched CSV")
    for row in originals.to_dict("records"):
        if any(str(contexts[row["line_id"]]["original"][key]) != value
               for key, value in row.items()):
            raise ValueError("Hospital 2 JSONL line fields disagree with CSV")

    submission, line_debug, invoice_debug, stats = audit_hospital_2(
        lines, invoices, contract, contexts)
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    csv_write(submission, output / "hospital_2_prediction.csv")
    print(f"Hospital 2: {len(submission)} invoices, {stats['Flagged invoices']} flagged. "
          "Saved outputs/hospital_2_prediction.csv")


UNIT_BASES = {"per day of service": "per_day", "per night of occupancy": "per_night",
              "per item supplied": "per_item", "per hour, per item": "per_hour_per_item"}


def audit_hospital_4(lines, invoices, contract, line_context=None, hospital=4):
    """Pure audit function for the structurally compatible H3/H4 contracts."""
    meta = contract["contract_metadata"]
    expected_contract = {3: "INS-H3-2024-0562", 4: "INS-H4-2024-2049"}
    if hospital not in expected_contract or meta["contract_number"] != expected_contract[hospital]:
        raise ValueError(f"Expected Hospital {hospital} contract")
    if hospital == 4 and any(s.get("weekend_uplift_percent") is not None for s in contract["services"]):
        raise ValueError("Hospital 4 has no weekend uplifts")
    if meta["rounding_convention"] != "half_up_cent":
        raise ValueError("Unsupported rounding convention")
    first = datetime.strptime(meta["effective_from"], "%d %B %Y").date()
    last = datetime.strptime(meta["effective_to"], "%d %B %Y").date()
    services = {s["service_name"]: s for s in contract["services"]}
    if len(services) != len(contract["services"]) or any(
            type(s.get("rate_cents")) is not int and not s.get("effective_rates")
            for s in services.values()):
        raise ValueError("Duplicate services or noninteger contract base rates")
    if lines["line_id"].duplicated().any():
        raise ValueError("line_id must be unique; repeated billing events with different IDs are retained")

    # Collapse headers to a unique join key without selecting conflicting values.
    headers, header_rows = {}, []
    for invoice_id, group in invoices.groupby("invoice_id", sort=False):
        records = group.to_dict("records")
        header = {"invoice_id": invoice_id}
        for field in invoices.columns:
            values = {r[field] for r in records}
            header[field] = next(iter(values)) if len(values) == 1 else None
        header["header_count"] = len(records)
        header["conflicting_fields"] = [f for f in invoices.columns if header[f] is None]
        header["submitted_totals"] = [integer(r["invoice_total_cents"], "invoice_total_cents") for r in records]
        header["contract_numbers"] = [r["contract_number"] for r in records]
        headers[invoice_id] = header
        header_rows.append(header)
    merged = lines.merge(pd.DataFrame(header_rows, dtype=object), on="invoice_id", how="left",
                         validate="many_to_one", sort=False, indicator=True)
    if len(merged) != len(lines):
        raise AssertionError("Join changed the number of invoice lines")
    rows = []
    for record in merged.to_dict("records"):
        joined = record.pop("_merge") == "both"
        if not joined:
            for field in invoices.columns:
                if field != "invoice_id":
                    record[field] = None
        if line_context is not None:
            context = line_context[record["line_id"]]
            for field in invoices.columns:
                if field != "invoice_id":
                    record[field] = context[field]
        record.update(errors=[], notes=[], joined=joined, rule=None)
        for field in ("quantity", "unit_price_cents", "line_total_cents"):
            record[field] = integer(record[field], field)
        if record["quantity"] < 0:
            raise ValueError(f"Negative quantities require credit-note semantics not specified by H{hospital}")
        record["day"] = parse_date(record["service_date"])
        record["invoice_day"] = parse_date(record.get("invoice_date"))
        record["in_term"] = record["day"] is not None and first <= record["day"] <= last
        if record["service_status"] in KNOWN_STATUSES:
            record["rule"] = services.get(record["matched_service"])
            if record["rule"] is None:
                record["notes"].append("Selected service missing from contract; service checks skipped")
            elif hospital == 3:
                record["rule"] = dict(record["rule"])
                effective = record["rule"].get("effective_rates", [])
                eligible = [entry for entry in effective if record["day"] is not None and
                            parse_date(entry["effective_from"]) <= record["day"]]
                record["rule"]["rate_cents"] = eligible[-1]["rate_cents"] if eligible else None
        elif record["service_status"] not in {"UNKNOWN", "UNSURE"}:
            raise ValueError(f"Unsupported service status: {record['service_status']}")
        if not joined:
            record["notes"].append("Missing invoice metadata join")
        elif headers[record["invoice_id"]]["conflicting_fields"]:
            record["notes"].append("Conflicting invoice metadata: " + ", ".join(headers[record["invoice_id"]]["conflicting_fields"]))
        if record["service_status"] == "UNSURE":
            record["notes"].append("Uncertain service: service-specific checks unavailable")
        record.update(expected_unit_price_cents=None, expected_line_total_cents=None,
                      billable_quantity=None, daily_quantity=None, cumulative_prior_quantity=None,
                      applied_discount_percent=0, applied_premiums=[], bundle_applies=False,
                      exclusion_applies=False, duplicate_event=False, price_ready=False,
                      later_duplicate=False,
                      price_alternatives={}, pricing_missing_reasons=[])
        rows.append(record)

    daily = defaultdict(list)
    patient_dates = defaultdict(list)
    by_invoice = defaultdict(list)
    for i, row in enumerate(rows):
        by_invoice[row["invoice_id"]].append(i)
        if row["rule"] and row["day"] and row.get("patient_id"):
            daily[(row["patient_id"], row["matched_service"], row["day"])].append(i)
            patient_dates[(row["patient_id"], row["matched_service"])].append(row["day"])
    for dates in patient_dates.values():
        dates.sort()
    for indexes in daily.values():
        total = sum(rows[i]["quantity"] for i in indexes)
        for i in indexes:
            rows[i]["daily_quantity"] = total

    # Audit assumption: contract-wide identified billed units, including duplicates.
    # H4 8.3-8.5 does not explicitly define scope or treatment of nonpayable units.
    running = Counter()
    for row in sorted((r for r in rows if r["rule"] and r["in_term"]),
                      key=lambda r: (r["day"], r["line_id"])):
        name = row["matched_service"]
        row["cumulative_prior_quantity"] = running[name]
        running[name] += row["quantity"]
    # A known volume service with an undatable line has uncertain placement.
    undated_services = {r["matched_service"] for r in rows if r["rule"] and not r["day"]}
    unknown_service_rows = [r for r in rows if r["service_status"] == "UNSURE" or
                            (r["service_status"] in KNOWN_STATUSES and not r["rule"])]
    unknown_context = [r for r in rows if r["rule"] and (not r["day"] or not r.get("patient_id"))]

    # Prepare contract rate components once; category checks below reuse them.
    for row in rows:
        rule = row["rule"]
        if not rule:
            continue
        missing = row["pricing_missing_reasons"]
        if row["day"] is not None and not row["in_term"]:
            missing.append("No contractual service date available")
        elif row["day"] is None:
            row["notes"].append("Provisional pricing despite malformed service date: contract-term eligibility unverified")
            if rule.get("rate_cents") is None:
                missing.append("Malformed date prevents selecting an effective-dated contract rate")
            if any(rule.get(k) is not None for k in
                   ("threshold_quantity", "bundle_with", "volume_threshold_1",
                    "daily_cap", "exclusion_with")):
                missing.append("Malformed date prevents determining date-dependent adjustments or billability")
        if not row["joined"]:
            missing.append("Missing invoice metadata")
        patient, day = row.get("patient_id"), row["day"]
        related = {row["matched_service"], rule.get("bundle_with"), rule.get("exclusion_with")}
        # Unidentified/contextless events may change daily, bundle or exclusion
        # eligibility. Unknown (uncontracted) services do not enter utilisation.
        possible_neighbors = [other for other in unknown_service_rows + unknown_context
                              if other is not row and
                              (not other.get("patient_id") or not patient or other["patient_id"] == patient) and
                              (not other["rule"] or other["matched_service"] in related) and
                              # H4 11.2 bounds a contract-valid service date by
                              # its invoice date, even when the line date is malformed.
                              # This excludes impossible overlap without inventing a date.
                              (other["day"] is not None or day is None or
                               other["invoice_day"] is None or
                               other["invoice_day"] >= day - timedelta(days=rule.get("exclusion_days") or 0)) and
                              (not other["day"] or not day or abs((other["day"] - day).days) <= (rule.get("exclusion_days") or 0))]
        contextual = rule.get("threshold_quantity") is not None or rule.get("bundle_with") is not None
        pricing_neighbors = [other for other in possible_neighbors if not other["rule"] or
                             (rule.get("threshold_quantity") is not None and other["matched_service"] == row["matched_service"] and
                              not (row["daily_quantity"] is not None and row["daily_quantity"] > rule["threshold_quantity"])) or
                             (other["matched_service"] == rule.get("bundle_with") and
                              (patient, rule.get("bundle_with"), day) not in daily)]
        if contextual and (not patient or pricing_neighbors):
            missing.append("Incomplete patient/day context for premiums or bundles")
        # Only an unknown triggering service can change an exclusion; an
        # undated occurrence of the excluded service itself cannot do so.
        billable_neighbors = [other for other in possible_neighbors
                              if not other["rule"] or
                              (rule.get("daily_cap") is not None and other["matched_service"] == row["matched_service"]) or
                              other["matched_service"] == rule.get("exclusion_with")]
        row["uncertain_billable_context"] = bool(billable_neighbors or not patient)
        row["bundle_applies"] = bool(patient and day and rule.get("bundle_with") and
                                        (patient, rule["bundle_with"], day) in daily)
        base = rule["bundled_rate_cents"] if row["bundle_applies"] else rule["rate_cents"]
        premiums = []
        if rule.get("threshold_quantity") is not None and row["daily_quantity"] is not None and row["daily_quantity"] > rule["threshold_quantity"]:
            premiums.append(rule["threshold_uplift_percent"])
        if rule.get("weekend_uplift_percent") is not None and day and day.weekday() >= 5:
            premiums.append(rule["weekend_uplift_percent"])
        prior = row["cumulative_prior_quantity"]
        discount = max([rule[f"volume_discount_{tier}_percent"] for tier in (1, 2)
                        if rule.get(f"volume_threshold_{tier}") is not None and prior is not None
                        and prior > rule[f"volume_threshold_{tier}"]] or [0])
        if rule.get("volume_threshold_1") is not None:
            if row["matched_service"] in undated_services or any(
                    not other["day"] or (other["in_term"] and day and
                    (other["day"], other["line_id"]) < (day, row["line_id"])) for other in unknown_service_rows):
                # The deepest tier remains certain if already reached even
                # without the unidentified units; otherwise abstain.
                deepest = max(rule.get("volume_discount_1_percent") or 0, rule.get("volume_discount_2_percent") or 0)
                if discount < deepest:
                    missing.append("Uncertain prior utilisation")
        row["applied_premiums"] = premiums
        row["applied_discount_percent"] = discount
        if missing:
            row["notes"].extend(missing)
            continue
        row["price_ready"] = True
        expected = price(base, premiums, discount)
        row["expected_unit_price_cents"] = expected
        service_discounts = {rule[f"volume_discount_{tier}_percent"] for tier in (1, 2)
                             if rule.get(f"volume_discount_{tier}_percent") is not None}
        # Evidence must identify an actual premium of THIS service whose
        # condition is false. Arbitrary rate increases remain generic mismatches.
        inactive_premiums = set()
        if rule.get("threshold_quantity") is not None and row["daily_quantity"] <= rule["threshold_quantity"]:
            inactive_premiums.add(rule["threshold_uplift_percent"])
        if rule.get("weekend_uplift_percent") is not None and day.weekday() < 5:
            inactive_premiums.add(rule["weekend_uplift_percent"])
        row["price_alternatives"] = {
            "without_discount": price(base, premiums),
            "without_bundle": price(rule["rate_cents"], premiums, discount),
            "omitted_premium": {price(base, premiums[:i] + premiums[i + 1:], discount) for i in range(len(premiums))},
            "wrong_discount": {price(base, premiums, d) for d in service_discounts if d != discount},
            "extra_premium": {price(base, premiums + [p], discount) for p in inactive_premiums},
        }

    # ============================================================
    # 1. cross_invoice_duplicate
    # ============================================================
    for indexes in daily.values():
        if len({rows[i]["invoice_id"] for i in indexes}) > 1:
            # Attribute rebilling to invoices issued strictly AFTER the earliest
            # dated invoice. With missing/tied dates, don't arbitrarily blame an
            # original: retain review notes and unavailable totals instead.
            days = [rows[i]["invoice_day"] for i in indexes]
            earliest = min(days) if all(days) else None
            earliest_ids = {rows[i]["invoice_id"] for i in indexes if rows[i]["invoice_day"] == earliest}
            for i in indexes:
                later = earliest is not None and rows[i]["invoice_day"] > earliest
                if later:
                    add_error(rows[i], "cross_invoice_duplicate")
                    rows[i]["later_duplicate"] = True
                    rows[i]["notes"].append("Rebilling after an earlier invoice for this patient/service/day")
                elif earliest is None or len(earliest_ids) > 1:
                    rows[i]["duplicate_event"] = True
                    rows[i]["notes"].append("Repeated event with ambiguous invoice chronology; attribution needs review")
        elif len(indexes) > 1:
            # Keep within-invoice repeats as review notes: the permitted 18
            # categories contain no corresponding error. Payable allocation is unclear.
            for i in indexes:
                rows[i]["duplicate_event"] = True
                rows[i]["notes"].append("Repeated service/day on one invoice; payable allocation unavailable")

    # ============================================================
    # 2. unit_price_mismatch
    # ============================================================
    for row in rows:
        if row["price_ready"] and row["unit_price_cents"] != row["expected_unit_price_cents"]:
            add_error(row, "unit_price_mismatch")

    # ============================================================
    # 3. wrong_unit_basis
    # ============================================================
    for row in rows:
        if row["rule"]:
            basis = row["rule"]["unit_basis"]
            expected_basis = UNIT_BASES.get(basis, basis.replace(" ", "_"))
            if row["unit_basis_as_billed"].strip().lower().replace(" ", "_") != expected_basis:
                add_error(row, "wrong_unit_basis")

    # ============================================================
    # 4. line_total_arithmetic
    # ============================================================
    for row in rows:
        if row["unit_price_cents"] * row["quantity"] != row["line_total_cents"]:
            add_error(row, "line_total_arithmetic")

    # ============================================================
    # 5. daily_cap_exceeded
    # ============================================================
    for row in rows:
        if row["rule"] and row["rule"].get("daily_cap") is not None and row["daily_quantity"] is not None:
            if row["daily_quantity"] > row["rule"]["daily_cap"]:
                add_error(row, "daily_cap_exceeded")

    # ============================================================
    # 6. service_date_after_invoice_date
    # ============================================================
    for row in rows:
        if row["day"] and row["invoice_day"] and row["day"] > row["invoice_day"]:
            add_error(row, "service_date_after_invoice_date")

    # ============================================================
    # 7. service_date_out_of_window
    # ============================================================
    for row in rows:
        if row["day"] and not row["in_term"]:
            add_error(row, "service_date_out_of_window")

    # ============================================================
    # 8. unknown_service
    # ============================================================
    for row in rows:
        if row["service_status"] == "UNKNOWN":
            add_error(row, "unknown_service")

    # ============================================================
    # 9. volume_discount_omitted
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and row["applied_discount_percent"] and
                row["unit_price_cents"] == row["price_alternatives"]["without_discount"]):
            add_error(row, "volume_discount_omitted")

    # ============================================================
    # 10. invoice_total_mismatch
    # ============================================================
    invoice_errors = {invoice_id: [] for invoice_id in headers}
    billed_sums = {invoice_id: sum(rows[i]["line_total_cents"] for i in indexes)
                   for invoice_id, indexes in by_invoice.items()}
    for invoice_id, header in headers.items():
        # Compare arithmetic to submitted total, NOT expected contract total.
        # Other categories describe pricing differences. Conflicting duplicate
        # totals cannot be associated with their individual line sets by ID alone.
        if line_context is not None:
            record_groups = defaultdict(list)
            for i in by_invoice.get(invoice_id, []):
                record_groups[line_context[rows[i]["line_id"]]["source_record"]].append(rows[i])
            if any(sum(r["line_total_cents"] for r in group) != integer(group[0]["invoice_total_cents"], "invoice_total_cents")
                   for group in record_groups.values()):
                invoice_errors[invoice_id].append("invoice_total_mismatch")
        elif header["invoice_total_cents"] is not None:
            if integer(header["invoice_total_cents"], "invoice_total_cents") != billed_sums.get(invoice_id, 0):
                invoice_errors[invoice_id].append("invoice_total_mismatch")

    # ============================================================
    # 11. exclusion_window_violation
    # ============================================================
    for row in rows:
        rule = row["rule"]
        if rule and rule.get("exclusion_with") and row["day"] and row.get("patient_id"):
            dates = patient_dates[(row["patient_id"], rule["exclusion_with"])]
            lower = row["day"] - timedelta(days=rule["exclusion_days"])
            position = bisect_left(dates, lower)
            if position < len(dates) and dates[position] <= row["day"] + timedelta(days=rule["exclusion_days"]):
                row["exclusion_applies"] = True
                add_error(row, "exclusion_window_violation")

    # ============================================================
    # 12. bundle_not_applied
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and row["bundle_applies"] and
                row["unit_price_cents"] == row["price_alternatives"]["without_bundle"]):
            add_error(row, "bundle_not_applied")

    # ============================================================
    # 13. contract_number_mismatch
    # ============================================================
    for invoice_id, header in headers.items():
        if any(number != meta["contract_number"] for number in header["contract_numbers"]):
            invoice_errors[invoice_id].append("contract_number_mismatch")

    # ============================================================
    # 14. premium_omitted
    # ============================================================
    for row in rows:
        if "unit_price_mismatch" in row["errors"] and row["unit_price_cents"] in row["price_alternatives"]["omitted_premium"]:
            add_error(row, "premium_omitted")

    # ============================================================
    # 15. volume_discount_incorrectly_applied
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and
                "volume_discount_omitted" not in row["errors"] and
                row["unit_price_cents"] in row["price_alternatives"]["wrong_discount"]):
            add_error(row, "volume_discount_incorrectly_applied")

    # ============================================================
    # 16. malformed_service_date
    # ============================================================
    for row in rows:
        if row["day"] is None:
            add_error(row, "malformed_service_date")

    # ============================================================
    # 17. premium_incorrectly_applied
    # ============================================================
    for row in rows:
        if "unit_price_mismatch" in row["errors"] and row["unit_price_cents"] in row["price_alternatives"]["extra_premium"]:
            add_error(row, "premium_incorrectly_applied")

    # ============================================================
    # 18. duplicate_invoice_id
    # ============================================================
    for invoice_id, header in headers.items():
        if header["header_count"] > 1:
            invoice_errors[invoice_id].append("duplicate_invoice_id")

    # Expected payable totals: known exclusions, later duplicate billings and uncontracted services are
    # nonbillable. Never manufacture a payable amount for UNSURE, invalid dates,
    # missing context or duplicate events whose invoice chronology is ambiguous.
    for row in rows:
        rule = row["rule"]
        if row["service_status"] == "UNKNOWN" or row["exclusion_applies"] or row["later_duplicate"] or (row["day"] is not None and not row["in_term"]):
            row["billable_quantity"] = 0
            row["expected_line_total_cents"] = 0
        elif row["price_ready"] and not row["duplicate_event"]:
            if rule.get("daily_cap") is not None or rule.get("exclusion_with"):
                if row.get("uncertain_billable_context"):
                    row["notes"].append("Incomplete context for cap/exclusion payable total")
                    continue
            quantity = min(row["quantity"], rule["daily_cap"]) if rule.get("daily_cap") is not None else row["quantity"]
            row["billable_quantity"] = quantity
            row["expected_line_total_cents"] = row["expected_unit_price_cents"] * quantity
        row["errors"] = ordered(row["errors"])

    submission, invoice_debug, line_debug = [], [], []
    for invoice_id in dict.fromkeys([*headers, *by_invoice]):
        header = headers.get(invoice_id)
        items = [rows[i] for i in by_invoice.get(invoice_id, [])]
        errors = ordered(set(invoice_errors.get(invoice_id, [])) | {e for r in items for e in reported_errors(r["errors"])})
        expected = None
        if items and all(r["expected_line_total_cents"] is not None for r in items) and header and (line_context is not None or not header["conflicting_fields"]):
            expected = sum(r["expected_line_total_cents"] for r in items)
        confidence, confidence_reason = submission_confidence(items, errors, expected, header)
        if expected is not None and any(r["day"] is None for r in items):
            confidence, confidence_reason = "0.80", "Provisional total: malformed service date; term eligibility unverified"
        billed = integer(header["invoice_total_cents"], "invoice_total_cents") if header and header["invoice_total_cents"] is not None else None
        if line_context is not None and header and header["header_count"] > 1:
            billed = sum(header["submitted_totals"])
        result = dict(invoice_id=invoice_id, flagged=int(bool(errors)), error_category=json.dumps(errors),
                      expected_total_cents=expected, billed_total_cents=billed, confidence=str(confidence))
        submission.append(result)
        invoice_debug.append({**result, "confidence_reason": confidence_reason,
                              "sum_billed_line_totals_cents": billed_sums.get(invoice_id, 0),
                              "source_invoice_records": header["header_count"] if header else 0,
                              "submitted_invoice_totals_cents": json.dumps(header["submitted_totals"] if header else []),
                              "conflicting_metadata": json.dumps(header["conflicting_fields"] if header else []),
                              "has_unsure_service": any(r["service_status"] == "UNSURE" for r in items),
                              "exact_total_available": expected is not None and all(r["day"] is not None for r in items),
                              "provisional_date_total": expected is not None and any(r["day"] is None for r in items)})
    for row in rows:
        fields = ["line_id", "invoice_id", "line_no", "service_date", "description", "matched_service",
                  "service_status", "confidence", "quantity", "unit_basis_as_billed", "unit_price_cents", "line_total_cents",
                  "expected_unit_price_cents", "billable_quantity", "expected_line_total_cents", "daily_quantity",
                  "cumulative_prior_quantity", "applied_discount_percent", "bundle_applies", "exclusion_applies"]
        line_errors = ordered(row["errors"] + invoice_errors.get(row["invoice_id"], []))
        line_debug.append({**{f: row.get(f) for f in fields}, "patient_id": row.get("patient_id"),
                           "invoice_date": row.get("invoice_date"), "flagged": int(bool(line_errors)),
                           "contract_base_rate_cents": row["rule"]["rate_cents"] if row["rule"] else None,
                           "contract_unit_basis": row["rule"]["unit_basis"] if row["rule"] else None,
                           "applied_premiums": json.dumps(row["applied_premiums"]),
                           "error_category": json.dumps(line_errors),
                           "reported_error_category": json.dumps(reported_errors(line_errors)),
                           "review_notes": json.dumps(row["notes"])})
    stats = {"Total invoices": len(invoices), "Distinct invoice IDs": len(headers), "Total line items": len(rows),
             "MATCHED rows": sum(r["service_status"] in KNOWN_STATUSES for r in rows),
             "MATCHED_BY_PRICE rows (included above)": sum(r["service_status"] == "MATCHED_BY_PRICE" for r in rows),
             "UNKNOWN rows": sum(r["service_status"] == "UNKNOWN" for r in rows),
             "UNSURE rows": sum(r["service_status"] == "UNSURE" for r in rows),
             "Flagged invoices": sum(r["flagged"] for r in submission),
             "Unmatched contract-service lookups": sum(r["service_status"] in KNOWN_STATUSES and not r["rule"] for r in rows),
             "Missing invoice joins": sum(not r["joined"] for r in rows),
             "Invoices with UNSURE services and unavailable expected total": sum(r["has_unsure_service"] and not r["exact_total_available"] for r in invoice_debug),
             "Invoices with unavailable expected total (all causes)": sum(r["expected_total_cents"] is None for r in invoice_debug),
             "Invoices with provisional malformed-date totals": sum(r["provisional_date_total"] for r in invoice_debug)}
    return submission, line_debug, invoice_debug, stats


def audit_hospital_3(lines, invoices, contract, line_context=None):
    return audit_hospital_4(lines, invoices, contract, line_context, hospital=3)


def run_hospital_3():
    lines = pd.read_csv(ROOT / "service matches/hospital_3_line_items_matched.csv",
                        dtype=str, keep_default_na=False)
    originals = pd.read_csv(ROOT / "invoices/hospital_3_line_items.csv", dtype=str,
                            keep_default_na=False)
    if not lines[list(originals.columns)].equals(originals):
        raise ValueError("Hospital 3 matched file differs from original invoice lines")
    invoices = pd.read_csv(ROOT / "invoices/hospital_3_invoices.csv", dtype=str,
                           keep_default_na=False)
    contract = json.loads((ROOT / "extracted contract rule/hospital_3_services.json")
                          .read_text(encoding="utf-8"))
    contexts = {}
    source_headers = []
    for index, text in enumerate((ROOT / "invoices/hospital_3_invoices.jsonl")
                                 .read_text(encoding="utf-8").splitlines()):
        source = json.loads(text)
        header = {key: str(source[key]) for key in invoices.columns}
        source_headers.append(header)
        for item in source["line_items"]:
            line_id = item["line_id"]
            if line_id in contexts:
                raise ValueError("Duplicate Hospital 3 line identifier in JSONL")
            contexts[line_id] = dict(header, source_record=index, original=item)
    if source_headers != invoices.to_dict("records"):
        raise ValueError("Hospital 3 JSONL headers disagree with invoice CSV")
    if set(contexts) != set(lines["line_id"]):
        raise ValueError("Hospital 3 JSONL line coverage disagrees with matched CSV")
    for row in originals.to_dict("records"):
        if any(str(contexts[row["line_id"]]["original"][key]) != value
               for key, value in row.items()):
            raise ValueError("Hospital 3 JSONL line fields disagree with CSV")

    submission, line_debug, invoice_debug, stats = audit_hospital_3(
        lines, invoices, contract, contexts)
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    csv_write(submission, output / "hospital_3_prediction.csv")
    print(f"Hospital 3: {len(submission)} invoices, {stats['Flagged invoices']} flagged. "
          "Saved outputs/hospital_3_prediction.csv")


def run_hospital_4():
    lines = pd.read_csv(ROOT / "service matches/hospital_4_line_items_matched.csv", dtype=str, keep_default_na=False)
    originals = pd.read_csv(ROOT / "invoices/hospital_4_line_items.csv", dtype=str, keep_default_na=False)
    if not lines[list(originals.columns)].equals(originals):
        raise ValueError("Matched file differs from original invoice lines")
    invoices = pd.read_csv(ROOT / "invoices/hospital_4_invoices.csv", dtype=str, keep_default_na=False)
    contract = json.loads((ROOT / "extracted contract rule/hospital_4_services.json").read_text(encoding="utf-8"))
    contexts = {}
    source_headers = []
    for index, text in enumerate((ROOT / "invoices/hospital_4_invoices.jsonl").read_text(encoding="utf-8").splitlines()):
        source = json.loads(text)
        header = {k: str(source[k]) for k in invoices.columns}
        source_headers.append(header)
        for item in source["line_items"]:
            if item["line_id"] in contexts:
                raise ValueError("Duplicate line identifier in JSONL")
            contexts[item["line_id"]] = dict(header, source_record=index, original=item)
    if source_headers != invoices.to_dict("records"):
        raise ValueError("JSONL headers disagree with invoice CSV")
    if set(contexts) != set(lines["line_id"]):
        raise ValueError("JSONL line coverage disagrees with CSV")
    for row in originals.to_dict("records"):
        if any(str(contexts[row["line_id"]]["original"][k]) != v for k, v in row.items()):
            raise ValueError("JSONL line fields disagree with CSV")
    submission, line_debug, invoice_debug, stats = audit_hospital_4(lines, invoices, contract, contexts)
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    csv_write(submission, output / "hospital_4_prediction.csv")
    print(f"Hospital 4: {len(submission)} invoices, {stats['Flagged invoices']} flagged. Saved outputs/hospital_4_prediction.csv")


def price_with_multipliers(base, premiums=(), discount=0, facility="1", tier="1"):
    for multiplier in (facility, tier):
        base = int((Decimal(base) * Decimal(multiplier)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    for premium in premiums:
        base = adjust(base, premium)
    return adjust(base, -discount)


def audit_hospital_5(lines, invoices, contract, line_context=None):
    """Pure audit function. Inputs are DataFrames and a parsed contract dictionary."""
    meta = contract["contract_metadata"]
    if meta["contract_number"] != "INS-H5-2024-0731":
        raise ValueError("Expected Hospital 5 contract")
    if meta["rounding_convention"] != "half_up_cent":
        raise ValueError("Unsupported rounding convention")
    first = datetime.strptime(meta["effective_from"], "%d %B %Y").date()
    last = datetime.strptime(meta["effective_to"], "%d %B %Y").date()
    services = {s["service_name"]: s for s in contract["services"]}
    if len(services) != len(contract["services"]) or any(type(s["rate_cents"]) is not int for s in services.values()):
        raise ValueError("Duplicate services or noninteger contract base rates")
    if lines["line_id"].duplicated().any():
        raise ValueError("line_id must be unique; repeated billing events with different IDs are retained")

    # Collapse headers to a unique join key without selecting conflicting values.
    headers, header_rows = {}, []
    for invoice_id, group in invoices.groupby("invoice_id", sort=False):
        records = group.to_dict("records")
        header = {"invoice_id": invoice_id}
        for field in invoices.columns:
            values = {r[field] for r in records}
            header[field] = next(iter(values)) if len(values) == 1 else None
        header["header_count"] = len(records)
        header["conflicting_fields"] = [f for f in invoices.columns if header[f] is None]
        header["submitted_totals"] = [integer(r["invoice_total_cents"], "invoice_total_cents") for r in records]
        header["contract_numbers"] = [r["contract_number"] for r in records]
        headers[invoice_id] = header
        header_rows.append(header)
    merged = lines.merge(pd.DataFrame(header_rows, dtype=object), on="invoice_id", how="left",
                         validate="many_to_one", sort=False, indicator=True)
    if len(merged) != len(lines):
        raise AssertionError("Join changed the number of invoice lines")
    rows = []
    for record in merged.to_dict("records"):
        joined = record.pop("_merge") == "both"
        if not joined:
            for field in invoices.columns:
                if field != "invoice_id":
                    record[field] = None
        if line_context is not None:
            context = line_context[record["line_id"]]
            for field in invoices.columns:
                if field != "invoice_id":
                    record[field] = context[field]
        record.update(errors=[], notes=[], joined=joined, rule=None)
        for field in ("quantity", "unit_price_cents", "line_total_cents"):
            record[field] = integer(record[field], field)
        if record["quantity"] < 0:
            raise ValueError("Negative quantities require credit-note semantics not specified by H5")
        record["day"] = parse_date(record["service_date"])
        record["invoice_day"] = parse_date(record.get("invoice_date"))
        record["in_term"] = record["day"] is not None and first <= record["day"] <= last
        if record["service_status"] in KNOWN_STATUSES:
            record["rule"] = services.get(record["matched_service"])
            if record["rule"] is None:
                record["notes"].append("Selected service missing from contract; service checks skipped")
        elif record["service_status"] not in {"UNKNOWN", "UNSURE"}:
            raise ValueError(f"Unsupported service status: {record['service_status']}")
        if not joined:
            record["notes"].append("Missing invoice metadata join")
        elif headers[record["invoice_id"]]["conflicting_fields"]:
            record["notes"].append("Conflicting invoice metadata: " + ", ".join(headers[record["invoice_id"]]["conflicting_fields"]))
        if record["service_status"] == "UNSURE":
            record["notes"].append("Uncertain service: service-specific checks unavailable")
        record.update(expected_unit_price_cents=None, expected_line_total_cents=None,
                      billable_quantity=None, daily_quantity=None, cumulative_prior_quantity=None,
                      applied_discount_percent=0, applied_premiums=[], bundle_applies=False,
                      exclusion_applies=False, duplicate_event=False, price_ready=False,
                      later_duplicate=False,
                      price_alternatives={}, pricing_missing_reasons=[])
        rows.append(record)

    daily = defaultdict(list)
    patient_dates = defaultdict(list)
    by_invoice = defaultdict(list)
    for i, row in enumerate(rows):
        by_invoice[row["invoice_id"]].append(i)
        if row["rule"] and row["day"] and row.get("patient_id"):
            daily[(row["patient_id"], row["matched_service"], row["day"])].append(i)
            patient_dates[(row["patient_id"], row["matched_service"])].append(row["day"])
    for dates in patient_dates.values():
        dates.sort()
    for indexes in daily.values():
        total = sum(rows[i]["quantity"] for i in indexes)
        for i in indexes:
            rows[i]["daily_quantity"] = total

    # H5 8.1: term-wide across patients with no facility/tier reset.
    # Interpretation: prior-line billed units, including nonpayable units.
    running = Counter()
    for row in sorted((r for r in rows if r["rule"] and r["in_term"]),
                      key=lambda r: (r["day"], r["line_id"])):
        name = row["matched_service"]
        row["cumulative_prior_quantity"] = running[name]
        running[name] += row["quantity"]
    # A known volume service with an undatable line has uncertain placement.
    undated_services = {r["matched_service"] for r in rows if r["rule"] and not r["day"]}
    unknown_service_rows = [r for r in rows if r["service_status"] == "UNSURE" or
                            (r["service_status"] in KNOWN_STATUSES and not r["rule"])]
    unknown_context = [r for r in rows if r["rule"] and (not r["day"] or not r.get("patient_id"))]

    # Prepare contract rate components once; category checks below reuse them.
    for row in rows:
        rule = row["rule"]
        if not rule:
            continue
        missing = row["pricing_missing_reasons"]
        facility = rule["facility_multipliers"].get(row.get("facility_code"))
        tier = rule["plan_tier_multipliers"].get(row.get("plan_tier"))
        if facility is None or tier is None:
            raise ValueError(f"Unknown facility/tier for {row['line_id']}")
        def effective_price(base, premiums=(), discount=0):
            return price_with_multipliers(base, premiums, discount, facility, tier)
        if row["day"] is not None and not row["in_term"]:
            missing.append("No contractual service date available")
        elif row["day"] is None:
            row["notes"].append("Provisional pricing despite malformed service date: contract-term eligibility unverified")
            if any(rule.get(k) is not None for k in
                   ("threshold_quantity", "bundle_with", "volume_threshold_1",
                    "daily_cap", "exclusion_with", "weekend_uplift_percent")):
                missing.append("Malformed date prevents determining date-dependent adjustments or billability")
        if not row["joined"]:
            missing.append("Missing invoice metadata")
        patient, day = row.get("patient_id"), row["day"]
        related = {row["matched_service"], rule.get("bundle_with"), rule.get("exclusion_with")}
        # Unidentified/contextless events may change daily, bundle or exclusion
        # eligibility. Unknown (uncontracted) services do not enter utilisation.
        possible_neighbors = [other for other in unknown_service_rows + unknown_context
                              if other is not row and
                              (not other.get("patient_id") or not patient or other["patient_id"] == patient) and
                              (not other["rule"] or other["matched_service"] in related) and
                              # H5 10.2 bounds a contract-valid service date by
                              # its invoice date, even when the line date is malformed.
                              # This excludes impossible overlap without inventing a date.
                              (other["day"] is not None or day is None or
                               other["invoice_day"] is None or
                               other["invoice_day"] >= day - timedelta(days=rule.get("exclusion_days") or 0)) and
                              (not other["day"] or not day or abs((other["day"] - day).days) <= (rule.get("exclusion_days") or 0))]
        contextual = rule.get("threshold_quantity") is not None or rule.get("bundle_with") is not None
        pricing_neighbors = [other for other in possible_neighbors if not other["rule"] or
                             (rule.get("threshold_quantity") is not None and other["matched_service"] == row["matched_service"] and
                              not (row["daily_quantity"] is not None and row["daily_quantity"] > rule["threshold_quantity"])) or
                             (other["matched_service"] == rule.get("bundle_with") and
                              (patient, rule.get("bundle_with"), day) not in daily)]
        if contextual and (not patient or pricing_neighbors):
            missing.append("Incomplete patient/day context for premiums or bundles")
        # Only an unknown triggering service can change an exclusion; an
        # undated occurrence of the excluded service itself cannot do so.
        billable_neighbors = [other for other in possible_neighbors
                              if not other["rule"] or
                              (rule.get("daily_cap") is not None and other["matched_service"] == row["matched_service"]) or
                              other["matched_service"] == rule.get("exclusion_with")]
        row["uncertain_billable_context"] = bool(billable_neighbors or not patient)
        row["bundle_applies"] = bool(patient and day and rule.get("bundle_with") and
                                        (patient, rule["bundle_with"], day) in daily)
        base = rule["bundled_rate_cents"] if row["bundle_applies"] else rule["rate_cents"]
        premiums = []
        if rule.get("threshold_quantity") is not None and row["daily_quantity"] is not None and row["daily_quantity"] > rule["threshold_quantity"]:
            premiums.append(rule["threshold_uplift_percent"])
        if rule.get("weekend_uplift_percent") is not None and day and day.weekday() >= 5:
            premiums.append(rule["weekend_uplift_percent"])
        prior = row["cumulative_prior_quantity"]
        discount = max([rule[f"volume_discount_{tier}_percent"] for tier in (1, 2)
                        if rule.get(f"volume_threshold_{tier}") is not None and prior is not None
                        and prior > rule[f"volume_threshold_{tier}"]] or [0])
        if rule.get("volume_threshold_1") is not None:
            if row["matched_service"] in undated_services or any(
                    not other["day"] or (other["in_term"] and day and
                    (other["day"], other["line_id"]) < (day, row["line_id"])) for other in unknown_service_rows):
                # The deepest tier remains certain if already reached even
                # without the unidentified units; otherwise abstain.
                deepest = max(rule.get("volume_discount_1_percent") or 0, rule.get("volume_discount_2_percent") or 0)
                if discount < deepest:
                    missing.append("Uncertain prior utilisation")
        row["applied_premiums"] = premiums
        row["applied_discount_percent"] = discount
        if missing:
            row["notes"].extend(missing)
            continue
        row["price_ready"] = True
        expected = effective_price(base, premiums, discount)
        row["expected_unit_price_cents"] = expected
        service_discounts = {rule[f"volume_discount_{tier}_percent"] for tier in (1, 2)
                             if rule.get(f"volume_discount_{tier}_percent") is not None}
        # Evidence must identify an actual premium of THIS service whose
        # condition is false. Arbitrary rate increases remain generic mismatches.
        inactive_premiums = set()
        if rule.get("threshold_quantity") is not None and row["daily_quantity"] <= rule["threshold_quantity"]:
            inactive_premiums.add(rule["threshold_uplift_percent"])
        if rule.get("weekend_uplift_percent") is not None and day and day.weekday() < 5:
            inactive_premiums.add(rule["weekend_uplift_percent"])
        row["price_alternatives"] = {
            "without_discount": effective_price(base, premiums),
            "without_bundle": effective_price(rule["rate_cents"], premiums, discount),
            "omitted_premium": {effective_price(base, premiums[:i] + premiums[i + 1:], discount) for i in range(len(premiums))},
            "wrong_discount": {effective_price(base, premiums, d) for d in service_discounts if d != discount},
            "extra_premium": {effective_price(base, premiums + [p], discount) for p in inactive_premiums},
        }

    # ============================================================
    # 1. cross_invoice_duplicate
    # ============================================================
    for indexes in daily.values():
        if len({rows[i]["invoice_id"] for i in indexes}) > 1:
            # Attribute rebilling to invoices issued strictly AFTER the earliest
            # dated invoice. With missing/tied dates, don't arbitrarily blame an
            # original: retain review notes and unavailable totals instead.
            days = [rows[i]["invoice_day"] for i in indexes]
            earliest = min(days) if all(days) else None
            earliest_ids = {rows[i]["invoice_id"] for i in indexes if rows[i]["invoice_day"] == earliest}
            for i in indexes:
                later = earliest is not None and rows[i]["invoice_day"] > earliest
                if later:
                    add_error(rows[i], "cross_invoice_duplicate")
                    rows[i]["later_duplicate"] = True
                    rows[i]["notes"].append("Rebilling after an earlier invoice for this patient/service/day")
                elif earliest is None or len(earliest_ids) > 1:
                    rows[i]["duplicate_event"] = True
                    rows[i]["notes"].append("Repeated event with ambiguous invoice chronology; attribution needs review")
        elif len(indexes) > 1:
            # Keep within-invoice repeats as review notes: the permitted 18
            # categories contain no corresponding error. Payable allocation is unclear.
            for i in indexes:
                rows[i]["duplicate_event"] = True
                rows[i]["notes"].append("Repeated service/day on one invoice; payable allocation unavailable")

    # ============================================================
    # 2. unit_price_mismatch
    # ============================================================
    for row in rows:
        if row["price_ready"] and row["unit_price_cents"] != row["expected_unit_price_cents"]:
            add_error(row, "unit_price_mismatch")

    # ============================================================
    # 3. wrong_unit_basis
    # ============================================================
    for row in rows:
        if row["rule"]:
            basis = row["rule"]["unit_basis"]
            expected_basis = UNIT_BASES.get(basis, basis.replace(" ", "_"))
            if row["unit_basis_as_billed"].strip().lower().replace(" ", "_") != expected_basis:
                add_error(row, "wrong_unit_basis")

    # ============================================================
    # 4. line_total_arithmetic
    # ============================================================
    for row in rows:
        if row["unit_price_cents"] * row["quantity"] != row["line_total_cents"]:
            add_error(row, "line_total_arithmetic")

    # ============================================================
    # 5. daily_cap_exceeded
    # ============================================================
    for row in rows:
        if row["rule"] and row["rule"].get("daily_cap") is not None and row["daily_quantity"] is not None:
            if row["daily_quantity"] > row["rule"]["daily_cap"]:
                add_error(row, "daily_cap_exceeded")

    # ============================================================
    # 6. service_date_after_invoice_date
    # ============================================================
    for row in rows:
        if row["day"] and row["invoice_day"] and row["day"] > row["invoice_day"]:
            add_error(row, "service_date_after_invoice_date")

    # ============================================================
    # 7. service_date_out_of_window
    # ============================================================
    for row in rows:
        if row["day"] and not row["in_term"]:
            add_error(row, "service_date_out_of_window")

    # ============================================================
    # 8. unknown_service
    # ============================================================
    for row in rows:
        if row["service_status"] == "UNKNOWN":
            add_error(row, "unknown_service")

    # ============================================================
    # 9. volume_discount_omitted
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and row["applied_discount_percent"] and
                row["unit_price_cents"] == row["price_alternatives"]["without_discount"]):
            add_error(row, "volume_discount_omitted")

    # ============================================================
    # 10. invoice_total_mismatch
    # ============================================================
    invoice_errors = {invoice_id: [] for invoice_id in headers}
    billed_sums = {invoice_id: sum(rows[i]["line_total_cents"] for i in indexes)
                   for invoice_id, indexes in by_invoice.items()}
    for invoice_id, header in headers.items():
        # Compare arithmetic to submitted total, NOT expected contract total.
        # Other categories describe pricing differences. Conflicting duplicate
        # totals cannot be associated with their individual line sets by ID alone.
        if line_context is not None:
            record_groups = defaultdict(list)
            for i in by_invoice.get(invoice_id, []):
                record_groups[line_context[rows[i]["line_id"]]["source_record"]].append(rows[i])
            if any(sum(r["line_total_cents"] for r in group) != integer(group[0]["invoice_total_cents"], "invoice_total_cents")
                   for group in record_groups.values()):
                invoice_errors[invoice_id].append("invoice_total_mismatch")
        elif header["invoice_total_cents"] is not None:
            if integer(header["invoice_total_cents"], "invoice_total_cents") != billed_sums.get(invoice_id, 0):
                invoice_errors[invoice_id].append("invoice_total_mismatch")

    # ============================================================
    # 11. exclusion_window_violation
    # ============================================================
    for row in rows:
        rule = row["rule"]
        if rule and rule.get("exclusion_with") and row["day"] and row.get("patient_id"):
            dates = patient_dates[(row["patient_id"], rule["exclusion_with"])]
            lower = row["day"] - timedelta(days=rule["exclusion_days"])
            position = bisect_left(dates, lower)
            if position < len(dates) and dates[position] <= row["day"] + timedelta(days=rule["exclusion_days"]):
                row["exclusion_applies"] = True
                add_error(row, "exclusion_window_violation")

    # ============================================================
    # 12. bundle_not_applied
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and row["bundle_applies"] and
                row["unit_price_cents"] == row["price_alternatives"]["without_bundle"]):
            add_error(row, "bundle_not_applied")

    # ============================================================
    # 13. contract_number_mismatch
    # ============================================================
    for invoice_id, header in headers.items():
        if any(number != meta["contract_number"] for number in header["contract_numbers"]):
            invoice_errors[invoice_id].append("contract_number_mismatch")

    # ============================================================
    # 14. premium_omitted
    # ============================================================
    for row in rows:
        if "unit_price_mismatch" in row["errors"] and row["unit_price_cents"] in row["price_alternatives"]["omitted_premium"]:
            add_error(row, "premium_omitted")

    # ============================================================
    # 15. volume_discount_incorrectly_applied
    # ============================================================
    for row in rows:
        if ("unit_price_mismatch" in row["errors"] and
                "volume_discount_omitted" not in row["errors"] and
                row["unit_price_cents"] in row["price_alternatives"]["wrong_discount"]):
            add_error(row, "volume_discount_incorrectly_applied")

    # ============================================================
    # 16. malformed_service_date
    # ============================================================
    for row in rows:
        if row["day"] is None:
            add_error(row, "malformed_service_date")

    # ============================================================
    # 17. premium_incorrectly_applied
    # ============================================================
    for row in rows:
        if "unit_price_mismatch" in row["errors"] and row["unit_price_cents"] in row["price_alternatives"]["extra_premium"]:
            add_error(row, "premium_incorrectly_applied")

    # ============================================================
    # 18. duplicate_invoice_id
    # ============================================================
    for invoice_id, header in headers.items():
        if header["header_count"] > 1:
            invoice_errors[invoice_id].append("duplicate_invoice_id")

    # Expected payable totals: known exclusions, later duplicate billings and uncontracted services are
    # nonbillable. Never manufacture a payable amount for UNSURE, invalid dates,
    # missing context or duplicate events whose invoice chronology is ambiguous.
    for row in rows:
        rule = row["rule"]
        if row["service_status"] == "UNKNOWN" or row["exclusion_applies"] or row["later_duplicate"] or (row["day"] is not None and not row["in_term"]):
            row["billable_quantity"] = 0
            row["expected_line_total_cents"] = 0
        elif row["price_ready"] and not row["duplicate_event"]:
            if rule.get("daily_cap") is not None or rule.get("exclusion_with"):
                if row.get("uncertain_billable_context"):
                    row["notes"].append("Incomplete context for cap/exclusion payable total")
                    continue
            quantity = min(row["quantity"], rule["daily_cap"]) if rule.get("daily_cap") is not None else row["quantity"]
            row["billable_quantity"] = quantity
            row["expected_line_total_cents"] = row["expected_unit_price_cents"] * quantity
        row["errors"] = ordered(row["errors"])

    submission, invoice_debug, line_debug = [], [], []
    for invoice_id in dict.fromkeys([*headers, *by_invoice]):
        header = headers.get(invoice_id)
        items = [rows[i] for i in by_invoice.get(invoice_id, [])]
        errors = ordered(set(invoice_errors.get(invoice_id, [])) | {e for r in items for e in reported_errors(r["errors"])})
        expected = None
        if items and all(r["expected_line_total_cents"] is not None for r in items) and header and (line_context is not None or not header["conflicting_fields"]):
            expected = sum(r["expected_line_total_cents"] for r in items)
        confidence, confidence_reason = submission_confidence(items, errors, expected, header)
        if expected is not None and any(r["day"] is None for r in items):
            confidence, confidence_reason = "0.80", "Provisional total: malformed service date; term eligibility unverified"
        billed = integer(header["invoice_total_cents"], "invoice_total_cents") if header and header["invoice_total_cents"] is not None else None
        if line_context is not None and header and header["header_count"] > 1:
            billed = sum(header["submitted_totals"])
        result = dict(invoice_id=invoice_id, flagged=int(bool(errors)), error_category=json.dumps(errors),
                      expected_total_cents=expected, billed_total_cents=billed, confidence=str(confidence))
        submission.append(result)
        invoice_debug.append({**result, "confidence_reason": confidence_reason,
                              "sum_billed_line_totals_cents": billed_sums.get(invoice_id, 0),
                              "source_invoice_records": header["header_count"] if header else 0,
                              "submitted_invoice_totals_cents": json.dumps(header["submitted_totals"] if header else []),
                              "conflicting_metadata": json.dumps(header["conflicting_fields"] if header else []),
                              "has_unsure_service": any(r["service_status"] == "UNSURE" for r in items),
                              "exact_total_available": expected is not None and all(r["day"] is not None for r in items),
                              "provisional_date_total": expected is not None and any(r["day"] is None for r in items)})
    for row in rows:
        fields = ["line_id", "invoice_id", "line_no", "service_date", "description", "matched_service",
                  "service_status", "confidence", "quantity", "unit_basis_as_billed", "unit_price_cents", "line_total_cents",
                  "expected_unit_price_cents", "billable_quantity", "expected_line_total_cents", "daily_quantity",
                  "cumulative_prior_quantity", "applied_discount_percent", "bundle_applies", "exclusion_applies"]
        line_errors = ordered(row["errors"] + invoice_errors.get(row["invoice_id"], []))
        line_debug.append({**{f: row.get(f) for f in fields}, "patient_id": row.get("patient_id"),
                           "invoice_date": row.get("invoice_date"), "flagged": int(bool(line_errors)),
                           "contract_base_rate_cents": row["rule"]["rate_cents"] if row["rule"] else None,
                           "contract_unit_basis": row["rule"]["unit_basis"] if row["rule"] else None,
                           "applied_premiums": json.dumps(row["applied_premiums"]),
                           "error_category": json.dumps(line_errors),
                           "reported_error_category": json.dumps(reported_errors(line_errors)),
                           "review_notes": json.dumps(row["notes"])})
    stats = {"Total invoices": len(invoices), "Distinct invoice IDs": len(headers), "Total line items": len(rows),
             "MATCHED rows": sum(r["service_status"] in KNOWN_STATUSES for r in rows),
             "MATCHED_BY_PRICE rows (included above)": sum(r["service_status"] == "MATCHED_BY_PRICE" for r in rows),
             "UNKNOWN rows": sum(r["service_status"] == "UNKNOWN" for r in rows),
             "UNSURE rows": sum(r["service_status"] == "UNSURE" for r in rows),
             "Flagged invoices": sum(r["flagged"] for r in submission),
             "Unmatched contract-service lookups": sum(r["service_status"] in KNOWN_STATUSES and not r["rule"] for r in rows),
             "Missing invoice joins": sum(not r["joined"] for r in rows),
             "Invoices with UNSURE services and unavailable expected total": sum(r["has_unsure_service"] and not r["exact_total_available"] for r in invoice_debug),
             "Invoices with unavailable expected total (all causes)": sum(r["expected_total_cents"] is None for r in invoice_debug),
             "Invoices with provisional malformed-date totals": sum(r["provisional_date_total"] for r in invoice_debug)}
    return submission, line_debug, invoice_debug, stats


def run_hospital_5():
    lines = pd.read_csv(ROOT / "service matches/hospital_5_line_items_matched.csv", dtype=str, keep_default_na=False)
    originals = pd.read_csv(ROOT / "invoices/hospital_5_line_items.csv", dtype=str, keep_default_na=False)
    if not lines[list(originals.columns)].equals(originals):
        raise ValueError("Matched file differs from original invoice lines")
    invoices = pd.read_csv(ROOT / "invoices/hospital_5_invoices.csv", dtype=str, keep_default_na=False)
    contract = json.loads((ROOT / "extracted contract rule/hospital_5_services.json").read_text(encoding="utf-8"))
    contexts = {}
    source_headers = []
    for index, text in enumerate((ROOT / "invoices/hospital_5_invoices.jsonl").read_text(encoding="utf-8").splitlines()):
        source = json.loads(text)
        header = {k: str(source[k]) for k in invoices.columns}
        source_headers.append(header)
        for item in source["line_items"]:
            if item["line_id"] in contexts:
                raise ValueError("Duplicate line identifier in JSONL")
            contexts[item["line_id"]] = dict(header, source_record=index, original=item)
    if source_headers != invoices.to_dict("records"):
        raise ValueError("JSONL headers disagree with invoice CSV")
    if set(contexts) != set(lines["line_id"]):
        raise ValueError("JSONL line coverage disagrees with CSV")
    for row in originals.to_dict("records"):
        if any(str(contexts[row["line_id"]]["original"][k]) != v for k, v in row.items()):
            raise ValueError("JSONL line fields disagree with CSV")
    submission, line_debug, invoice_debug, stats = audit_hospital_5(lines, invoices, contract, contexts)
    output = ROOT / "outputs"
    output.mkdir(exist_ok=True)
    csv_write(submission, output / "hospital_5_prediction.csv")
    print(f"Hospital 5: {len(submission)} invoices, {stats['Flagged invoices']} flagged. Saved outputs/hospital_5_prediction.csv")


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def build_submission():
    columns, _ = read_csv(ROOT / "submission_template.csv")
    combined = []
    for hospital in (2, 3, 4, 5):
        fields, rows = read_csv(ROOT / f"outputs/hospital_{hospital}_prediction.csv")
        if fields != columns:
            raise ValueError(f"Hospital {hospital}: columns differ from template")
        _, headers = read_csv(ROOT / f"invoices/hospital_{hospital}_invoices.csv")
        ids = list(dict.fromkeys(row["invoice_id"] for row in headers))
        if [row["invoice_id"] for row in rows] != ids:
            raise ValueError(f"Hospital {hospital}: missing, repeated or reordered invoice IDs")
        billed = {}
        for header in headers:
            key = header["invoice_id"]
            billed[key] = billed.get(key, 0) + int(header["invoice_total_cents"])
        for row in rows:
            if row["flagged"] not in ("0", "1"):
                raise ValueError(f"Invalid flag: {row}")
            categories = json.loads(row["error_category"])
            if not isinstance(categories, list) or any(not isinstance(c, str) for c in categories):
                raise ValueError(f"Invalid categories: {row}")
            if bool(categories) != (row["flagged"] == "1"):
                raise ValueError(f"Flag/category disagreement: {row}")
            for field in ("expected_total_cents", "billed_total_cents"):
                if field == "expected_total_cents" and row[field] == "":
                    continue
                if not re.fullmatch(r"-?\d+", row[field]):
                    raise ValueError(f"Noninteger money: {row}")
            if int(row["billed_total_cents"]) != billed[row["invoice_id"]]:
                raise ValueError(f"Billed total does not reconcile: {row}")
            confidence = Decimal(row["confidence"])
            if not confidence.is_finite() or not 0 <= confidence <= 1:
                raise ValueError(f"Invalid confidence: {row}")
        combined.extend(rows)
    if len({row["invoice_id"] for row in combined}) != len(combined):
        raise ValueError("Repeated invoice IDs across hospitals")
    destination = ROOT / "outputs/submission.csv"
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(combined)
    missing = sum(row["expected_total_cents"] == "" for row in combined)
    print(f"Validated {len(combined)} rows; {missing} expected totals unavailable. Saved outputs/submission.csv")


def main():
    parser = argparse.ArgumentParser(description="Audit hospital invoices")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--hospital", type=int, choices=(1, 2, 3, 4, 5))
    target.add_argument("--all", action="store_true",
                        help="Audit Hospitals 1-5 and build the Hospital 2-5 submission")
    target.add_argument("--submission", action="store_true",
                        help="Audit Hospitals 2-5, then combine their predictions")
    target.add_argument("--build-submission", action="store_true",
                        help="Combine existing Hospital 2-5 predictions without rerunning audits")
    parser.add_argument("--evaluate", action="store_true", help="Hospital 1 evaluation")
    args = parser.parse_args()
    if args.evaluate and not (args.hospital == 1 or args.all):
        parser.error("--evaluate applies only with --hospital 1 or --all")
    if args.all:
        previous = sys.argv
        sys.argv = [previous[0]] + (["--evaluate"] if args.evaluate else [])
        try:
            run_hospital_1()
        finally:
            sys.argv = previous
        run_hospital_2()
        run_hospital_3()
        run_hospital_4()
        run_hospital_5()
        build_submission()
    elif args.submission:
        run_hospital_2()
        run_hospital_3()
        run_hospital_4()
        run_hospital_5()
        build_submission()
    elif args.build_submission:
        build_submission()
    elif args.hospital == 1:
        previous = sys.argv
        sys.argv = [previous[0]] + (["--evaluate"] if args.evaluate else [])
        try:
            run_hospital_1()
        finally:
            sys.argv = previous
    else:
        {2: run_hospital_2, 3: run_hospital_3, 4: run_hospital_4,
         5: run_hospital_5}[args.hospital]()


if __name__ == "__main__":
    main()
