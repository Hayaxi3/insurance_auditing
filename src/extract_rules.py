"""Shared helpers with explicit hospital-specific contract behavior.
Run with --hospital 1, 4 or 5.
"""

from pathlib import Path
import argparse
import json
import re
import warnings

ROOT = Path(__file__).resolve().parents[1]


SOURCE_1 = ROOT / "contracts/hospital_1/provider_services_agreement.md"


OUTPUT_1 = ROOT / "extracted contract rule/hospital_1_services.json"


RULE_FIELDS = (
    "daily_cap", "threshold_quantity", "threshold_uplift_percent",
    "weekend_uplift_percent", "volume_threshold_1", "volume_discount_1_percent",
    "volume_threshold_2", "volume_discount_2_percent", "bundle_with",
    "bundled_rate_cents", "exclusion_with", "exclusion_days",
)


def money_cents(value):
    """Convert exact GBP decimal text using integer arithmetic only."""
    match = re.fullmatch(r"GBP (\d+|\d{1,3}(?:,\d{3})+)\.(\d{2})", value)
    if not match:
        raise ValueError(f"Invalid GBP amount: {value!r}")
    return int(match[1].replace(",", "")) * 100 + int(match[2])


def quantity(value):
    match = re.fullmatch(r"(\d+) [a-z ]+", value)
    if not match:
        raise ValueError(f"Invalid quantity: {value!r}")
    return int(match[1])


def percent(value):
    if not re.fullmatch(r"\+?\d+%", value):
        raise ValueError(f"Invalid percentage: {value!r}")
    return int(value.rstrip("%"))


def table(section, expected_header):
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines() if line.strip().startswith("|")
    ]
    if (len(rows) < 3 or rows[0] != expected_header
            or not all(re.fullmatch(r":?-+:?", cell) for cell in rows[1])):
        raise ValueError(f"Missing or unexpected table: {expected_header}")
    if any(len(row) != len(expected_header) for row in rows):
        raise ValueError(f"Malformed table: {expected_header}")
    return rows[2:]


def extract_hospital_1(text):
    parts = re.split(r"^## \d+\. (.+)\n", text, flags=re.MULTILINE)
    sections = dict(zip(parts[1::2], parts[2::2]))
    metadata = {
        label.lower().replace(" ", "_"): value.strip()
        for label, value in re.findall(r"^\*\*(.+?):\*\* (.+)$", parts[0], re.MULTILINE)
    }
    for key in ("contract_number", "effective_from", "effective_to", "currency",
                "rounding_convention"):
        if not metadata.get(key):
            raise ValueError(f"Missing metadata: {key}")
    facility = re.search(r"single facility, (.+?) \(([^)]+)\)", sections["Parties and Term"])
    if not facility:
        raise ValueError("Missing facility metadata")
    metadata.update(facility_name=facility[1], facility_code=facility[2])
    # Keep exact prose defining scope, comparisons, ordering and rounding once.
    metadata["clauses"] = {
        title: [line.strip() for line in body.splitlines()
                if re.match(r"^\d+\.\d+ ", line)]
        for title, body in sections.items()
        if re.search(r"^\d+\.\d+ ", body, re.MULTILINE)
    }

    services = []
    for name, basis, rate, cap in table(
        sections["Rate Schedule"], ["Service", "Unit basis", "Rate", "Daily cap"]
    ):
        services.append(dict(
            service_name=name, unit_basis=basis, rate_cents=money_cents(rate),
            **{field: None for field in RULE_FIELDS},
        ))
        services[-1]["daily_cap"] = None if cap == "—" else quantity(cap)
    names = [row["service_name"] for row in services]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("Rate Schedule service names must be nonempty and unique")
    by_name = {row["service_name"]: row for row in services}

    def lookup(name):
        if name not in by_name:
            warnings.warn(f"Special-rule service absent from Rate Schedule: {name}", stacklevel=2)
        return by_name.get(name)

    def merge(name, **values):
        row = lookup(name)
        if row is not None:
            for key, value in values.items():
                if row[key] is not None and row[key] != value:
                    raise ValueError(f"Conflicting {key} for {name}")
                row[key] = value

    for name, limit, uplift in table(sections["Threshold Premiums"],
            ["Service", "Applies when daily quantity exceeds", "Uplift"]):
        merge(name, threshold_quantity=quantity(limit), threshold_uplift_percent=percent(uplift))
    for name, uplift in table(sections["Non-Business-Day Uplifts"],
            ["Service", "Uplift where the Service Date is not a Business Day"]):
        merge(name, weekend_uplift_percent=percent(uplift))
    volume_counts = {}
    for name, limit, discount in table(sections["Cumulative Volume Discounts"],
            ["Service", "Cumulative utilisation exceeds", "Discount on subsequent units"]):
        slot = volume_counts.get(name, 0) + 1
        volume_counts[name] = slot
        if slot > 2:
            raise ValueError(f"More than two volume thresholds for {name}")
        merge(name, **{f"volume_threshold_{slot}": quantity(limit),
                       f"volume_discount_{slot}_percent": percent(discount)})
    caps = {}
    for name, cap in table(sections["Daily Quantity Caps"],
            ["Service", "Maximum billable units per Patient per Service Day"]):
        caps[name] = quantity(cap)
        merge(name, daily_cap=caps[name])
    for row in services:
        if row["daily_cap"] is not None and caps.get(row["service_name"]) != row["daily_cap"]:
            raise ValueError(f"Daily cap tables disagree for {row['service_name']}")
    for a, b, rate_a, rate_b in table(sections["Bundled Services"],
            ["Service A", "Service B", "Bundled rate A", "Bundled rate B"]):
        merge(a, bundle_with=b, bundled_rate_cents=money_cents(rate_a))
        merge(b, bundle_with=a, bundled_rate_cents=money_cents(rate_b))
    for name, days, related in table(sections["Exclusion Windows"],
            ["Service", "Not billable within", "Of this Service"]):
        lookup(related)
        merge(name, exclusion_with=related, exclusion_days=quantity(days))

    for row in services:
        if type(row.get("rate_cents")) is not int:
            raise ValueError(f"Missing or non-integer base rate: {row['service_name']}")
        if row["volume_threshold_2"] is not None:
            if row["volume_threshold_2"] <= row["volume_threshold_1"]:
                raise ValueError(f"Volume thresholds out of order: {row['service_name']}")
    return {"contract_metadata": metadata, "services": services}


SOURCE_4 = ROOT / "contracts/hospital_4/conditional_reimbursement_agreement.md"


OUTPUT_4 = ROOT / "extracted contract rule/hospital_4_services.json"


def parenthesized_integer(value, suffix=""):
    match = re.fullmatch(r"[a-z -]+ \((\d+)" + re.escape(suffix) + r"\)", value)
    if not match:
        raise ValueError(f"Invalid written-out number: {value!r}")
    return int(match[1])


def extract_hospital_4(text):
    parts = re.split(r"^## \d+\. (.+)\n", text, flags=re.MULTILINE)
    sections = dict(zip(parts[1::2], parts[2::2]))
    expected_sections = {
        "Definitions", "Term and Scope", "Base Rates", "Order of Adjustments",
        "Threshold Premiums", "Daily Quantity Limits", "Bundled Delivery",
        "Discounts", "Exclusion Windows", "Non-Business-Day Uplifts", "Invoicing",
    }
    if set(sections) != expected_sections or len(parts[1::2]) != len(sections):
        raise ValueError("Missing, duplicate or unexpected contract sections")
    metadata = {
        label.lower().replace(" ", "_"): value.strip()
        for label, value in re.findall(r"^\*\*(.+?):\*\* (.+)$", parts[0], re.MULTILINE)
    }
    for key in ("contract_number", "provider", "payer", "effective_from",
                "effective_to", "currency", "rounding_convention"):
        if not metadata.get(key):
            raise ValueError(f"Missing metadata: {key}")
    if metadata["currency"] != "GBP":
        raise ValueError("Expected GBP currency")
    facility = re.search(r"All Services are delivered from (.+?) \(([^)]+)\)",
                         sections["Term and Scope"])
    if not facility:
        raise ValueError("Missing facility metadata")
    metadata.update(facility_name=facility[1], facility_code=facility[2])
    metadata["source_file"] = SOURCE_4.relative_to(ROOT).as_posix()
    metadata["preamble"] = [line for line in parts[0].splitlines() if line.startswith("_")]
    metadata["clauses"] = {
        title: [line.strip() for line in body.splitlines()
                if re.match(r"^\d+\.\d+ ", line)]
        for title, body in sections.items()
    }
    # Section 10 explicitly has no uplift table. Preserve that fact, too.
    if sections["Non-Business-Day Uplifts"].strip().splitlines()[-1] != "_None._":
        raise ValueError("Expected no non-business-day uplifts")
    if "|" in sections["Non-Business-Day Uplifts"]:
        raise ValueError("Unexpected non-business-day uplift table")
    metadata["non_business_day_uplifts"] = []

    services = [dict(service_name=name, unit_basis=basis, rate_cents=money_cents(rate),
                     **{field: None for field in RULE_FIELDS})
                for name, basis, rate in table(sections["Base Rates"],
                                              ["Service", "Unit basis", "Base rate"])]
    by_name = {row["service_name"]: row for row in services}
    if len(by_name) != len(services) or "" in by_name:
        raise ValueError("Service names must be nonempty and unique")

    def merge(name, **values):
        if name not in by_name:
            raise ValueError(f"Rule references unknown service: {name}")
        row = by_name[name]
        for key, value in values.items():
            if row[key] is not None:
                raise ValueError(f"Duplicate {key} for {name}")
            row[key] = value

    for name, limit, uplift in table(sections["Threshold Premiums"],
            ["Service", "Threshold (per Patient per Service Day)", "Uplift"]):
        if not limit.startswith("more than "):
            raise ValueError(f"Unexpected threshold comparison: {limit}")
        merge(name, threshold_quantity=quantity(limit.removeprefix("more than ")),
              threshold_uplift_percent=percent(uplift))
    for name, cap in table(sections["Daily Quantity Limits"],
            ["Service", "Maximum units per Patient per Service Day"]):
        merge(name, daily_cap=quantity(cap))
    for a, rate_a, b, rate_b in table(sections["Bundled Delivery"],
            ["Service A", "Substituted rate A", "Service B", "Substituted rate B"]):
        if a == b:
            raise ValueError(f"Self-referencing bundle: {a}")
        merge(a, bundle_with=b, bundled_rate_cents=money_cents(rate_a))
        merge(b, bundle_with=a, bundled_rate_cents=money_cents(rate_b))
    volume_counts = {}
    for name, limit, discount in table(sections["Discounts"],
            ["Service", "Cumulative utilisation exceeds", "Discount on subsequent instances"]):
        slot = volume_counts.get(name, 0) + 1
        volume_counts[name] = slot
        if slot > 2:
            raise ValueError(f"More than two volume thresholds for {name}")
        merge(name, **{f"volume_threshold_{slot}": parenthesized_integer(limit),
                       f"volume_discount_{slot}_percent": parenthesized_integer(discount, "%")})
    for name, days, related in table(sections["Exclusion Windows"],
            ["Service", "Window", "Excluded by delivery of"]):
        if related not in by_name:
            raise ValueError(f"Exclusion references unknown service: {related}")
        merge(name, exclusion_with=related, exclusion_days=quantity(days))
    for row in services:
        if row["volume_threshold_2"] is not None:
            if (row["volume_threshold_2"] <= row["volume_threshold_1"] or
                    row["volume_discount_2_percent"] <= row["volume_discount_1_percent"]):
                raise ValueError(f"Volume tiers out of order: {row['service_name']}")
    return {"contract_metadata": metadata, "services": services}


SOURCE_5 = ROOT / 'contracts/hospital_5/network_reimbursement_agreement.md'


OUTPUT_5 = ROOT / 'extracted contract rule/hospital_5_services.json'


def extract_hospital_5(text):
    parts = re.split(r'^## (\d+)\. (.+)\n', text, flags=re.M)
    sections = {int(parts[i]): parts[i+2] for i in range(1,len(parts),3)}
    titles = {int(parts[i]): parts[i+1] for i in range(1,len(parts),3)}
    if set(sections) != set(range(1,11)) or len(parts) != 31:
        raise ValueError('Unexpected or duplicate sections')
    metadata = {k.lower().replace(' ','_'):v.strip() for k,v in
                re.findall(r'^\*\*(.+?):\*\* (.+)$',parts[0],re.M)}
    for key in ('contract_number','provider','payer','effective_from','effective_to','currency','rounding_convention'):
        if not metadata.get(key):
            raise ValueError(f'Missing metadata: {key}')
    if metadata['currency'] != 'GBP':
        raise ValueError('Expected GBP')
    metadata['source_file'] = SOURCE_5.relative_to(ROOT).as_posix()
    metadata['preamble'] = [x for x in parts[0].splitlines() if x.startswith('_')]
    metadata['clauses'] = {titles[n]:re.findall(r'^\d+\.\d+ .+$',body,re.M) for n,body in sections.items()}
    facilities = table(sections[1],['Facility code','Facility'])
    metadata['facilities'] = dict(facilities)
    metadata['plan_tiers'] = ['BRONZE','SILVER','GOLD']
    if 'BRONZE, SILVER, GOLD' not in sections[1] or len(dict(facilities)) != len(facilities):
        raise ValueError('Unexpected facility or plan list')
    metadata['multiplier_encoding'] = 'Exact decimal strings; use Decimal, rounding half up after each step.'
    metadata['extraction_notes'] = [
        'Facility wording refers to line items in 1.2/Table 2 and invoices in 10.1; original clauses are retained without choosing a data fallback.',
        'Section 9 gives exclusion windows but does not explicitly state direction or patient scope; no Hospital 1 or Hospital 4 interpretation is imported.',
        'Section 8 states subsequent units, ordering and global term utilisation; it does not explicitly specify prior-line versus within-line threshold crossing.'
    ]
    rates = re.split(r'^### (.+)\n',sections[4],flags=re.M)
    if len(rates)!=5 or rates[1]!='Table 2 — Facility Multipliers' or rates[3]!='Table 3 — Plan-Tier Multipliers':
        raise ValueError('Unexpected multiplier tables')
    metadata['table_notes'] = {rates[i]:[x.strip() for x in rates[i+1].splitlines() if x.strip() and not x.startswith('|')]
                               for i in (1,3)}
    services = []
    for name,basis,rate,cap in table(rates[0],['Service','Unit basis','Base rate','Daily cap']):
        row = dict(service_name=name,unit_basis=basis,rate_cents=money_cents(rate),
                   **{k:None for k in RULE_FIELDS})
        row['daily_cap'] = None if cap=='—' else quantity(cap)
        services.append(row)
    by_name = {r['service_name']:r for r in services}
    if '' in by_name or len(by_name)!=len(services):
        raise ValueError('Duplicate/empty services')
    def merge(name, **values):
        if name not in by_name:
            raise ValueError(f'Unknown rule service: {name}')
        for k,v in values.items():
            if by_name[name].get(k) is not None:
                raise ValueError(f'Duplicate rule: {name}/{k}')
            by_name[name][k]=v
    for body,field,columns in ((rates[2],'facility_multipliers',list(metadata['facilities'])),
                               (rates[4],'plan_tier_multipliers',metadata['plan_tiers'])):
        seen=set()
        for name,*values in table(body,['Service',*columns]):
            if name in seen or any(not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',v) or not any(c in '123456789' for c in v) for v in values):
                raise ValueError('Duplicate service or invalid multiplier')
            merge(name,**{field:dict(zip(columns,values))})
            seen.add(name)
        if seen!=set(by_name):
            raise ValueError('Incomplete multiplier coverage')
    def threshold(s):
        if not s.startswith('more than '):
            raise ValueError(f'Unexpected comparison: {s}')
        return quantity(s.removeprefix('more than '))
    for name,limit,uplift in table(sections[5],['Service','Daily quantity threshold','Uplift']):
        merge(name,threshold_quantity=threshold(limit),threshold_uplift_percent=percent(uplift))
    for name,uplift in table(sections[6],['Service','Uplift']):
        merge(name,weekend_uplift_percent=percent(uplift))
    for a,ra,b,rb in table(sections[7],['Service A','Substituted rate A','Service B','Substituted rate B']):
        if a==b:
            raise ValueError('Self bundle')
        merge(a,bundle_with=b,bundled_rate_cents=money_cents(ra))
        merge(b,bundle_with=a,bundled_rate_cents=money_cents(rb))
    counts={}
    for name,limit,discount in table(sections[8],['Service','Cumulative utilisation','Discount on subsequent units']):
        slot=counts.get(name,0)+1
        if slot>2:
            raise ValueError('More than two volume tiers')
        counts[name]=slot
        merge(name,**{f'volume_threshold_{slot}':threshold(limit),f'volume_discount_{slot}_percent':percent(discount)})
    for name,window,trigger in table(sections[9],['Service','Not billable within','Of this Service']):
        if trigger not in by_name:
            raise ValueError(f'Unknown exclusion trigger: {trigger}')
        merge(name,exclusion_with=trigger,exclusion_days=quantity(window))
    for r in services:
        if r['volume_threshold_2'] is not None and not (r['volume_threshold_2']>r['volume_threshold_1'] and r['volume_discount_2_percent']>r['volume_discount_1_percent']):
            raise ValueError('Volume tiers out of order')
    return dict(contract_metadata=metadata,services=services)


def main():
    parser = argparse.ArgumentParser(description="Extract hospital contract rules")
    parser.add_argument("--hospital", type=int, choices=(1, 4, 5), required=True)
    args = parser.parse_args()
    source, output, extract = {
        1: (SOURCE_1, OUTPUT_1, extract_hospital_1),
        4: (SOURCE_4, OUTPUT_4, extract_hospital_4),
        5: (SOURCE_5, OUTPUT_5, extract_hospital_5),
    }[args.hospital]
    result = extract(source.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Extracted {len(result['services'])} services to {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
