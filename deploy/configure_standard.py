#!/usr/bin/env python3
"""Apply the standard, official AyP Frappe/ERPNext/HRMS setup idempotently."""
from __future__ import annotations

import os
from pathlib import Path

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

SITE = os.environ.get("AYP_SITE_NAME", "hr.aroypedal.com")
COMPANY = "ARO Y PEDAL SRL"
ADMIN_EMAIL = "juanluis@aroypedal.com"
SECRETS_DIR = Path(os.environ.get("AYP_SECRETS_DIR", "/run/ayp-secrets"))


def read_secret(name: str) -> str:
    env_name = f"AYP_{name.upper()}"
    value = os.environ.get(env_name, "").strip()
    if not value and (SECRETS_DIR / name).is_file():
        value = (SECRETS_DIR / name).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Required secret {name} is empty")
    return value


def ensure_company_address() -> str:
    existing = frappe.db.get_value(
        "Dynamic Link",
        {"link_doctype": "Company", "link_name": COMPANY, "parenttype": "Address"},
        "parent",
    )
    if existing:
        return existing

    address = frappe.get_doc(
        {
            "doctype": "Address",
            "address_title": "Aro y Pedal - Santo Domingo",
            "address_type": "Office",
            "address_line1": "Ave. 27 de Febrero 112",
            "city": "Santo Domingo",
            "state": "Distrito Nacional",
            "pincode": "10202",
            "country": "Dominican Republic",
            "email_id": ADMIN_EMAIL,
            "is_your_company_address": 1,
            "links": [{"link_doctype": "Company", "link_name": COMPANY}],
        }
    )
    address.insert(ignore_permissions=True)
    return address.name


def main() -> None:
    frappe.init(site=SITE)
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        if not frappe.is_setup_complete():
            result = setup_complete(
                {
                    "language": "Spanish",
                    "lang": "Spanish",
                    "country": "Dominican Republic",
                    "timezone": "America/Santo_Domingo",
                    "currency": "DOP",
                    "email": ADMIN_EMAIL,
                    "full_name": "Juan Rodríguez",
                    "password": read_secret("admin_password"),
                    "company_name": COMPANY,
                    "company_abbr": "AYP",
                    "chart_of_accounts": "Standard",
                    "fy_start_date": "2026-01-01",
                    "fy_end_date": "2026-12-31",
                    "domain": "",
                    "bank_account": "",
                    "setup_demo": 0,
                    "enable_telemetry": 0,
                }
            )
            if result and result.get("status") != "ok":
                raise RuntimeError(f"Unexpected setup result: {result}")

        if not frappe.db.exists("Company", COMPANY):
            raise RuntimeError(f"Setup completed without creating company {COMPANY}")

        address = ensure_company_address()
        frappe.db.set_value("User", ADMIN_EMAIL, "language", "es", update_modified=False)
        frappe.db.set_value(
            "User", ADMIN_EMAIL, "time_zone", "America/Santo_Domingo", update_modified=False
        )
        frappe.db.commit()
        print(
            {
                "status": "configured",
                "site": SITE,
                "company": COMPANY,
                "address": address,
                "country": "Dominican Republic",
                "currency": "DOP",
                "time_zone": "America/Santo_Domingo",
                "language": "es",
                "demo_data": False,
            }
        )
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()
