from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime
from pathlib import Path
import argparse
import csv
import json
import re


ROOT = Path(__file__).resolve().parents[1]

OUTPUT_FIELDS = (
    "line_id", "invoice_id", "line_no", "service_date", "description",
    "quantity", "unit_basis_as_billed", "unit_price_cents", "line_total_cents",
    "cleaned_description", "matched_service", "service_status", "confidence", "reason",
)

QUALIFIERS = {
    "advanced", "ambulatory", "assisted", "bedside", "comprehensive", "continuous",
    "elective", "emergency", "extended", "focused", "inpatient", "intensive",
    "intermittent", "outpatient", "postoperative", "preoperative", "routine",
    "specialist", "standard", "supervised",
}

SPECIALTIES = {
    "cardiac", "dermatologic", "endocrine", "gastrointestinal", "geriatric",
    "haematology", "hepatic", "immunologic", "infectious", "metabolic",
    "musculoskeletal", "neurological", "obstetric", "oncology", "ophthalmic",
    "orthopaedic", "otolaryngologic", "paediatric", "palliative", "psychiatric",
    "pulmonary", "renal", "rheumatologic", "urologic", "vascular",
}

# These words describe the outer billing form.  Missing one is weaker evidence
# than contradicting a specialty or the clinical concept itself.
GENERIC_SHELL = {"occupancy", "procedure", "service", "session", "therapy"}

# Versioned, domain-readable vocabulary.  It is not learned from labels or a
# saved description-to-service lookup and therefore also applies to unseen rows.
ABBREVIATIONS = {
    "adv": "advanced", "amb": "ambulatory", "asst": "assisted", "beds": "bedside",
    "compr": "comprehensive", "cont": "continuous", "elect": "elective",
    "emer": "emergency", "ext": "extended", "foc": "focused", "inpt": "inpatient",
    "intens": "intensive", "interm": "intermittent", "outpt": "outpatient",
    "postop": "postoperative", "preop": "preoperative", "rtn": "routine",
    "spclst": "specialist", "std": "standard", "supv": "supervised",
    "card": "cardiac", "derm": "dermatologic", "endo": "endocrine",
    "ent": "otolaryngologic", "ger": "geriatric", "gi": "gastrointestinal",
    "haem": "haematology", "hep": "hepatic", "immun": "immunologic",
    "infect": "infectious", "metab": "metabolic", "msk": "musculoskeletal",
    "neuro": "neurological", "obst": "obstetric", "onc": "oncology",
    "ophth": "ophthalmic", "ortho": "orthopaedic", "paed": "paediatric",
    "pall": "palliative", "psych": "psychiatric", "pulm": "pulmonary",
    "ren": "renal", "rheum": "rheumatologic", "urol": "urologic", "vasc": "vascular",
    "admin": "administration", "anaes": "anaesthesia", "anly": "analysis",
    "bd": "bed", "biop": "biopsy", "conf": "conference", "consult": "consultation",
    "cr": "care", "crit": "critical", "cs": "case", "diag": "diagnostic",
    "dial": "dialysis", "disch": "discharge", "disp": "dispensing",
    "endosc": "endoscopic", "fract": "fraction", "hm": "home", "img": "imaging",
    "inf": "infusion", "interp": "interpretation", "isol": "isolation",
    "lab": "laboratory", "monit": "monitoring", "nurs": "nursing",
    "nutr": "nutritional", "obs": "observation", "occ": "occupancy",
    "pharm": "pharmaceutical", "physio": "physiotherapy", "plng": "planning",
    "pnl": "panel", "proc": "procedure", "prog": "programme",
    "radiother": "radiotherapy", "recov": "recovery", "rehab": "rehabilitation",
    "rm": "room", "sess": "session", "spcm": "specimen", "steril": "sterilisation",
    "supp": "support", "svc": "service", "telem": "telemetry", "ther": "therapy",
    "thtr": "theatre", "tm": "time", "transf": "transfusion",
    "transp": "transport", "vent": "ventilation", "vst": "visit", "wd": "ward",
    "wnd": "wound",
}

UNIT_BASES = {
    "per day of service": "per_day",
    "per night of occupancy": "per_night",
    "per item supplied": "per_item",
    "per hour, per item": "per_hour_per_item",
}


def cleaned_description(value, hospital):
    suffixes = {1: "NG", 2: "SA", 3: "RM", 4: "CW", 5: "PH"}
    suffix = suffixes[hospital]
    value = re.sub(rf"\s*/{suffix}-\d{{4}}\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*[-|]+\s*", " ", value)
    return " ".join(value.split())


def tokens(value):
    return [ABBREVIATIONS.get(token, token)
            for token in re.findall(r"[a-z]+", value.lower())]


def normalized_unit(value):
    return value.strip().lower().replace(" ", "_")


def contract_unit(value):
    return UNIT_BASES.get(value, value.replace(" ", "_"))


def adjust(cents, percent):
    return int((Decimal(cents) * (Decimal(100) + Decimal(percent)) / 100)
               .quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def effective_rate(rule, service_date=None):
    """Select the contractual base rate for a service date, if determinable."""
    rates = rule.get("effective_rates")
    if not rates:
        return rule.get("rate_cents")
    try:
        day = date.fromisoformat(service_date or "")
    except ValueError:
        return None
    eligible = [entry for entry in rates if date.fromisoformat(entry["effective_from"]) <= day]
    return eligible[-1]["rate_cents"] if eligible else None


def possible_prices(rule, service_date=None, facility_code=None, plan_tier=None):
    """Return exact documented price possibilities, never nearest prices."""
    if rule.get("effective_rates"):
        selected = effective_rate(rule, service_date)
        bases = {selected} if selected is not None else set()
    else:
        bases = {rule["rate_cents"]}
    if rule.get("bundled_rate_cents") is not None:
        bases.add(rule["bundled_rate_cents"])
    # Contract adjustment order is threshold premium, then weekend uplift.
    premiums = [value for value in
                (rule.get("threshold_uplift_percent"), rule.get("weekend_uplift_percent"))
                if value is not None]
    discounts = {0, *[value for value in
                       (rule.get("volume_discount_1_percent"),
                        rule.get("volume_discount_2_percent")) if value is not None]}
    results = set()
    premium_sets = [()]
    for premium in premiums:
        premium_sets += [values + (premium,) for values in list(premium_sets)]
    for base in bases:
        facility = rule.get("facility_multipliers", {}).get(facility_code, "1")
        tier = rule.get("plan_tier_multipliers", {}).get(plan_tier, "1")
        base = int((Decimal(base) * Decimal(facility)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))
        base = int((Decimal(base) * Decimal(tier)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))
        for selected in premium_sets:
            priced = base
            for premium in selected:
                priced = adjust(priced, premium)
            for discount in discounts:
                results.add(adjust(priced, -discount))
    return results


def textual_candidates(description, services):
    description_tokens = set(tokens(description))
    stated_qualifiers = description_tokens & QUALIFIERS
    stated_specialties = description_tokens & SPECIALTIES
    core = description_tokens - QUALIFIERS - SPECIALTIES
    candidates = []
    for service in services:
        service_tokens = service["_tokens"]
        if stated_qualifiers and not stated_qualifiers <= service_tokens:
            continue
        if stated_specialties and not stated_specialties <= service_tokens:
            continue
        if (core - service_tokens) - GENERIC_SHELL:
            continue
        matched = description_tokens & service_tokens
        evidence = sum(3 if token in core else 4 for token in matched)
        coverage = Decimal(len(matched)) / max(1, len(description_tokens))
        score = Decimal(evidence) + coverage - Decimal("0.15") * len(service_tokens - description_tokens)
        candidates.append((score, service))
    return sorted(candidates, key=lambda item: (-item[0], item[1]["service_name"]))


def match_line(row, services, hospital=1):
    cleaned = cleaned_description(row["description"], hospital)
    candidates = textual_candidates(cleaned, services)
    if not candidates:
        return cleaned, "UNKNOWN", "UNKNOWN", "0.00", (
            "No contracted service is compatible with all explicit qualifier, specialty, "
            "and service-concept terms."
        )

    if hospital == 3:
        service_date = row.get("service_date", "")
        try:
            day = date.fromisoformat(service_date)
        except ValueError:
            day = None
        contract_from = services[0].get("_contract_effective_from") if services else None
        contract_to = services[0].get("_contract_effective_to") if services else None
        if day is not None and contract_from and contract_to and not (
                date.fromisoformat(contract_from) <= day <= date.fromisoformat(contract_to)):
            return cleaned, "UNKNOWN", "UNKNOWN", "0.00", (
                "Service date falls outside the contract term."
            )
        eligible = []
        date_uncertain = []
        for score, service in candidates:
            boundary = service.get("contracted_from")
            if not boundary:
                eligible.append((score, service))
            elif day is None:
                date_uncertain.append((score, service))
            elif day >= date.fromisoformat(boundary):
                eligible.append((score, service))
        if not eligible:
            if date_uncertain:
                return cleaned, "UNSURE", "UNSURE", "0.50", (
                    "Service eligibility depends on a missing or malformed service date."
                )
            return cleaned, "UNKNOWN", "UNKNOWN", "0.00", (
                "The described service was not contracted on the supplied service date."
            )
        candidates = eligible

    best_score = candidates[0][0]
    leaders = [service for score, service in candidates if best_score - score < Decimal("0.30")]
    if len(leaders) == 1:
        selected = leaders[0]
        return cleaned, selected["service_name"], "MATCHED", "0.97", (
            f"Unique textual candidate from normalized contract terminology; score={best_score}."
        )

    billed_unit = normalized_unit(row["unit_basis_as_billed"])
    unit_candidates = [service for service in leaders
                       if contract_unit(service["unit_basis"]) == billed_unit]
    if len(unit_candidates) == 1:
        selected = unit_candidates[0]
        return cleaned, selected["service_name"], "MATCHED", "0.93", (
            "Multiple textual candidates remained; billed unit uniquely supported this candidate. "
            f"Text candidates={[service['service_name'] for service in leaders]}."
        )
    remaining = unit_candidates or leaders

    billed_price = int(row["unit_price_cents"])
    price_candidates = [service for service in remaining
                        if billed_price in possible_prices(
                            service, row.get("service_date"), row.get("facility_code"),
                            row.get("plan_tier"))]
    if len(price_candidates) == 1:
        selected = price_candidates[0]
        return cleaned, selected["service_name"], "MATCHED_BY_PRICE", "0.90", (
            "Text left multiple plausible candidates and unit evidence did not resolve them; "
            f"the exact billed unit price {billed_price} matched a documented price possibility "
            f"for this candidate. Text candidates={[service['service_name'] for service in leaders]}."
        )

    names = [service["service_name"] for service in remaining]
    return cleaned, "UNSURE", "UNSURE", "0.50", (
        f"Multiple textually plausible candidates remain after unit and exact-price evidence: {names}."
    )


def match_hospital(hospital, contract_path=None, line_path=None, output_path=None):
    if hospital not in (1, 2, 3, 4, 5):
        raise ValueError("The reproducible matcher is currently validated for Hospitals 1-5")
    contract_path = contract_path or ROOT / f"extracted contract rule/hospital_{hospital}_services.json"
    line_path = line_path or ROOT / f"invoices/hospital_{hospital}_line_items.csv"
    output_path = output_path or ROOT / f"service matches/hospital_{hospital}_line_items_matched.csv"
    contract_path = Path(contract_path).resolve()
    line_path = Path(line_path).resolve()
    output_path = Path(output_path).resolve()
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    services = contract["services"]
    metadata = contract["contract_metadata"]
    contract_from = datetime.strptime(metadata["effective_from"], "%d %B %Y").date().isoformat()
    contract_to = datetime.strptime(metadata["effective_to"], "%d %B %Y").date().isoformat()
    for service in services:
        service["_tokens"] = set(tokens(service["service_name"]))
        service["_possible_prices"] = possible_prices(service)
        if hospital == 3:
            service["_contract_effective_from"] = contract_from
            service["_contract_effective_to"] = contract_to
    with Path(line_path).open(encoding="utf-8-sig", newline="") as stream:
        source = list(csv.DictReader(stream))
    if not source or tuple(source[0]) != OUTPUT_FIELDS[:9]:
        raise ValueError("Unexpected line-item columns")
    if len({row["line_id"] for row in source}) != len(source):
        raise ValueError("line_id must be unique")

    contexts = {}
    if hospital == 5 and line_path == (ROOT / "invoices/hospital_5_line_items.csv").resolve():
        jsonl_path = ROOT / "invoices/hospital_5_invoices.jsonl"
        for text in jsonl_path.read_text(encoding="utf-8").splitlines():
            invoice = json.loads(text)
            for item in invoice["line_items"]:
                contexts[item["line_id"]] = {
                    "facility_code": invoice["facility_code"],
                    "plan_tier": invoice["plan_tier"],
                }
        if set(contexts) != {row["line_id"] for row in source}:
            raise ValueError("Hospital 5 JSONL context does not cover every line item")

    output = []
    for row in source:
        match_input = {**row, **contexts.get(row["line_id"], {})}
        cleaned, service, status, confidence, reason = match_line(match_input, services, hospital)
        output.append({**row, "cleaned_description": cleaned, "matched_service": service,
                       "service_status": status, "confidence": confidence, "reason": reason})
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with Path(output_path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output)
    try:
        display_path = output_path.relative_to(ROOT)
    except ValueError:
        display_path = output_path
    print(f"Hospital {hospital}: matched {len(output)} lines to {len(services)} contract services. "
          f"Saved {display_path}")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--hospital", type=int, choices=(1, 2, 3, 4, 5),
                        help="Match one hospital")
    target.add_argument("--all", action="store_true", help="Match Hospitals 1-5")
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--line-items", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.all and any((args.contract, args.line_items, args.output)):
        parser.error("custom paths can only be used with --hospital")
    for hospital in range(1, 6) if args.all else (args.hospital,):
        match_hospital(hospital, args.contract, args.line_items, args.output)


if __name__ == "__main__":
    main()
