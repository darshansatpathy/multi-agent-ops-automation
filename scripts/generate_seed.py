"""Generate the fictional operations dataset in data/ops_seed.json.

Aurora Freight Ltd (fictional) runs road freight in the Benelux, Germany, France,
Spain and Sweden. Every record is invented. The data is hand-shaped so that each
evaluation scenario hits a specific branch of policy: an SLA credit under the
auto-approval limit, one that needs approval, one above the hard limit, a rebooking
that is allowed, one that costs too much, a duplicate invoice, damaged goods, and so on.

Run: python scripts/generate_seed.py
"""

from __future__ import annotations

import json
from pathlib import Path

AS_OF = "2026-11-02"

CUSTOMERS = [
    ("C-1001", "Nordwind Retail BV", "gold", "NL", "ops@nordwind-retail.example"),
    ("C-1002", "Hanse Electronics GmbH", "silver", "DE", "logistics@hanse-electronics.example"),
    ("C-1003", "Lumen Pharma NV", "gold", "BE", "supply@lumen-pharma.example"),
    ("C-1004", "Atlas Home SARL", "standard", "FR", "transport@atlas-home.example"),
    ("C-1005", "Polar Foods AB", "silver", "SE", "freight@polar-foods.example"),
    ("C-1006", "Iberia Motion SL", "standard", "ES", "envios@iberia-motion.example"),
]

CARRIERS = [
    ("CR-01", "Rijn Road Freight", ["NLRTM-DEHAM", "NLRTM-BEANR"]),
    ("CR-02", "Baltic Line Trucking", ["NLRTM-DEHAM", "DEHAM-SESTO"]),
    ("CR-03", "Flandria Express", ["BEANR-FRPAR", "NLRTM-BEANR"]),
    ("CR-04", "Sud Transportes", ["FRPAR-ESMAD"]),
    ("CR-05", "Nordic Haul", ["DEHAM-SESTO"]),
    ("CR-06", "Alpen Cargo", ["NLRTM-DEHAM", "BEANR-FRPAR"]),
]

# Spare capacity: (carrier, lane, pickup date, transit days, rate EUR)
CAPACITY = [
    ("CR-02", "NLRTM-DEHAM", "2026-11-02", 1, 1950),
    ("CR-06", "NLRTM-DEHAM", "2026-11-03", 1, 2400),
    ("CR-03", "NLRTM-BEANR", "2026-11-02", 1, 4400),
    ("CR-06", "BEANR-FRPAR", "2026-11-02", 1, 2300),
    ("CR-05", "DEHAM-SESTO", "2026-11-03", 2, 3100),
]

# id, customer, lane, carrier, status, promised, eta (or delivered), freight cost, cargo value, note
SHIPMENTS = [
    (
        "AF-24001",
        "C-1001",
        "NLRTM-DEHAM",
        "CR-01",
        "delayed",
        "2026-11-03",
        "2026-11-05",
        1800,
        64000,
        "Tractor breakdown near Venlo",
    ),
    ("AF-24002", "C-1002", "NLRTM-DEHAM", "CR-02", "in_transit", "2026-11-04", "2026-11-04", 1700, 210000, ""),
    (
        "AF-24003",
        "C-1003",
        "NLRTM-BEANR",
        "CR-01",
        "delayed",
        "2026-11-02",
        "2026-11-04",
        4200,
        380000,
        "Driver hours exceeded, trailer parked",
    ),
    (
        "AF-24004",
        "C-1004",
        "BEANR-FRPAR",
        "CR-03",
        "delivered",
        "2026-10-28",
        "2026-10-30",
        1900,
        22000,
        "Delivered two days late",
    ),
    (
        "AF-24005",
        "C-1005",
        "DEHAM-SESTO",
        "CR-05",
        "exception_damaged",
        "2026-10-31",
        "2026-10-31",
        2600,
        48000,
        "Pallets crushed during transshipment",
    ),
    (
        "AF-24006",
        "C-1006",
        "FRPAR-ESMAD",
        "CR-04",
        "delayed",
        "2026-11-03",
        "2026-11-06",
        2100,
        35000,
        "Border congestion at Irun",
    ),
    (
        "AF-24007",
        "C-1001",
        "NLRTM-BEANR",
        "CR-03",
        "held_customs",
        "2026-11-03",
        "2026-11-04",
        900,
        15000,
        "Awaiting T1 document",
    ),
    ("AF-24008", "C-1002", "NLRTM-DEHAM", "CR-01", "delivered", "2026-10-25", "2026-10-25", 950, 41000, ""),
    (
        "AF-24009",
        "C-1005",
        "DEHAM-SESTO",
        "CR-02",
        "delivered",
        "2026-10-27",
        "2026-10-27",
        2800,
        52000,
        "Customer says fuel surcharge too high",
    ),
    (
        "AF-24010",
        "C-1003",
        "NLRTM-DEHAM",
        "CR-06",
        "delayed",
        "2026-11-02",
        "2026-11-06",
        26000,
        1900000,
        "Temperature-controlled convoy held",
    ),
    (
        "AF-24011",
        "C-1004",
        "BEANR-FRPAR",
        "CR-03",
        "delayed",
        "2026-11-03",
        "2026-11-05",
        1600,
        18000,
        "Vehicle shortage",
    ),
    ("AF-24012", "C-1006", "FRPAR-ESMAD", "CR-04", "delivered", "2026-10-20", "2026-10-20", 120, 2000, ""),
    (
        "AF-24013",
        "C-1002",
        "DEHAM-SESTO",
        "CR-05",
        "delayed",
        "2026-11-03",
        "2026-11-05",
        3000,
        74000,
        "Ferry cancellation",
    ),
    ("AF-24014", "C-1005", "NLRTM-DEHAM", "CR-02", "in_transit", "2026-11-03", "2026-11-03", 1500, 30000, ""),
    (
        "AF-24015",
        "C-1001",
        "NLRTM-DEHAM",
        "CR-01",
        "delivered",
        "2026-10-29",
        "2026-10-31",
        2000,
        51000,
        "Late delivery, already credited",
    ),
]

TRACKING = {
    "AF-24001": [("2026-11-01T06:10", "Picked up in Rotterdam"), ("2026-11-01T14:40", "Breakdown reported near Venlo")],
    "AF-24002": [("2026-11-02T07:00", "Picked up in Rotterdam"), ("2026-11-02T12:30", "Passed Oldenzaal")],
    "AF-24003": [("2026-11-01T05:00", "Picked up in Rotterdam"), ("2026-11-01T19:00", "Trailer parked, driver rest")],
    "AF-24005": [
        ("2026-10-30T08:00", "Picked up in Hamburg"),
        ("2026-10-31T09:15", "Damage recorded at Malmö cross-dock"),
    ],
    "AF-24006": [("2026-11-01T09:00", "Picked up in Paris"), ("2026-11-02T08:00", "Queue at Irun border")],
    "AF-24007": [("2026-11-02T06:00", "Arrived at Antwerp customs")],
    "AF-24010": [
        ("2026-11-01T04:00", "Convoy departed Rotterdam"),
        ("2026-11-01T22:00", "Convoy held for reefer inspection"),
    ],
    "AF-24011": [("2026-11-02T10:00", "Awaiting vehicle in Antwerp")],
    "AF-24013": [
        ("2026-11-01T11:00", "Picked up in Hamburg"),
        ("2026-11-02T06:00", "Ferry Travemünde-Trelleborg cancelled"),
    ],
    "AF-24014": [("2026-11-02T06:30", "Picked up in Rotterdam")],
}

# invoice id, shipment, amount, status
INVOICES = [
    ("INV-8001", "AF-24001", 1800, "open"),
    ("INV-8003", "AF-24003", 4200, "open"),
    ("INV-8004", "AF-24004", 1900, "paid"),
    ("INV-8008-A", "AF-24008", 950, "paid"),
    ("INV-8008-B", "AF-24008", 950, "paid"),
    ("INV-8009", "AF-24009", 2800, "disputed"),
    ("INV-8010", "AF-24010", 26000, "open"),
    ("INV-8012-A", "AF-24012", 120, "paid"),
    ("INV-8012-B", "AF-24012", 120, "paid"),
    ("INV-8013", "AF-24013", 3000, "open"),
    ("INV-8015", "AF-24015", 2000, "paid"),
]

# Credits already on file: invoice, amount, reason
PRIOR_CREDITS = [("INV-8015", 200, "sla_breach")]


def build() -> dict[str, object]:
    return {
        "as_of": AS_OF,
        "company": "Aurora Freight Ltd (fictional)",
        "customers": [{"id": i, "name": n, "tier": t, "country": c, "contact": e} for i, n, t, c, e in CUSTOMERS],
        "carriers": [{"id": i, "name": n, "lanes": lanes} for i, n, lanes in CARRIERS],
        "capacity": [
            {"carrier_id": c, "lane": lane, "pickup_date": d, "transit_days": t, "rate_eur": r}
            for c, lane, d, t, r in CAPACITY
        ],
        "shipments": [
            {
                "id": i,
                "customer_id": c,
                "lane": lane,
                "carrier_id": cr,
                "status": st,
                "promised_date": p,
                "eta_date": e,
                "freight_cost_eur": fc,
                "cargo_value_eur": cv,
                "internal_note": note,
            }
            for i, c, lane, cr, st, p, e, fc, cv, note in SHIPMENTS
        ],
        "tracking": {sid: [{"at": at, "event": ev} for at, ev in evs] for sid, evs in TRACKING.items()},
        "invoices": [{"id": i, "shipment_id": s, "amount_eur": a, "status": st} for i, s, a, st in INVOICES],
        "prior_credits": [{"invoice_id": i, "amount_eur": a, "reason": r} for i, a, r in PRIOR_CREDITS],
    }


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "data" / "ops_seed.json"
    out.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
