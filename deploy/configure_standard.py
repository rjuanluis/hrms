#!/usr/bin/env python3
"""Apply the standard, official AyP Frappe/ERPNext/HRMS setup idempotently."""
from __future__ import annotations

import os
from pathlib import Path

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.translate import set_default_language

SITE = os.environ.get("AYP_SITE_NAME", "hr.aroypedal.com")
COMPANY = "ARO Y PEDAL SRL"
TAX_ID = "101-57005-9"
ADMIN_EMAIL = "juanluis@aroypedal.com"
SITES_DIR = Path("/home/frappe/frappe-bench/sites")
SECRETS_DIR = Path(os.environ.get("AYP_SECRETS_DIR", "/run/ayp-secrets"))
BRANCHES = (
    {
        "name": "Tienda Principal",
        "address_line1": "Ave. 27 de Febrero 112, casi esq. Leopoldo Navarro",
        "city": "Santo Domingo",
        "state": "Distrito Nacional",
        "pincode": "10201",
        "phone": "829-903-6570",
        "email_id": "27defebrero@aroypedal.com",
    },
    {
        "name": "Tienda Kennedy",
        "address_line1": "Plaza Kennedy, Aut. Duarte Km 6 ½, Av. John F. Kennedy",
        "city": "Santo Domingo",
        "state": "Distrito Nacional",
        "pincode": "",
        "phone": "849-858-7657",
        "email_id": "kennedy@aroypedal.com",
    },
)
DEPARTMENTS = (
    "Gerencia",
    "Administración, Finanzas y RRHH",
    "Ventas",
    "Almacén y Logística",
    "Centros de Servicio",
    "Marketing",
)
DEPARTMENT_RENAMES = {
    "Dirección General": "Gerencia",
    "Administración": "Administración, Finanzas y RRHH",
    "Marketing y Ventas": "Marketing",
    "Taller y Servicio Técnico": "Centros de Servicio",
}


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


def ensure_branches() -> list[str]:
    names = []
    for item in BRANCHES:
        name = item["name"]
        if not frappe.db.exists("Branch", name):
            frappe.get_doc({"doctype": "Branch", "branch": name}).insert(ignore_permissions=True)

        existing_address = frappe.db.get_value(
            "Dynamic Link",
            {"link_doctype": "Branch", "link_name": name, "parenttype": "Address"},
            "parent",
        )
        if not existing_address:
            address = frappe.get_doc(
                {
                    "doctype": "Address",
                    "address_title": name,
                    "address_type": "Office",
                    "address_line1": item["address_line1"],
                    "city": item["city"],
                    "state": item["state"],
                    "pincode": item["pincode"],
                    "country": "Dominican Republic",
                    "phone": item["phone"],
                    "email_id": item["email_id"],
                    "is_your_company_address": 1,
                    "links": [
                        {"link_doctype": "Branch", "link_name": name},
                        {"link_doctype": "Company", "link_name": COMPANY},
                    ],
                }
            )
            address.insert(ignore_permissions=True)
        names.append(name)
    return names


def department_name(value: str) -> str | None:
    return frappe.db.get_value(
        "Department", {"department_name": value, "company": COMPANY}, "name"
    )


def ensure_departments() -> list[str]:
    department_meta = frappe.get_meta("Department")
    has_disabled = department_meta.has_field("disabled")

    for old_label, new_label in DEPARTMENT_RENAMES.items():
        old_name = department_name(old_label)
        new_name = department_name(new_label)
        if old_name and not new_name:
            frappe.rename_doc("Department", old_name, new_label, force=True)
            renamed = frappe.get_doc("Department", new_label)
            renamed.department_name = new_label
            if has_disabled:
                renamed.disabled = 0
            renamed.save(ignore_permissions=True)
        elif old_name and new_name and has_disabled:
            frappe.db.set_value("Department", old_name, "disabled", 1, update_modified=False)

    names: list[str] = []
    for label in DEPARTMENTS:
        name = department_name(label)
        if not name:
            doc = frappe.get_doc(
                {"doctype": "Department", "department_name": label, "company": COMPANY}
            )
            doc.insert(ignore_permissions=True)
            name = doc.name
        if has_disabled:
            frappe.db.set_value("Department", name, "disabled", 0, update_modified=False)
        names.append(name)

    if has_disabled:
        desired_names = set(names)
        company_departments = frappe.get_all(
            "Department", filters={"company": COMPANY}, fields=["name", "is_group"]
        )
        for department in company_departments:
            if department.name not in desired_names and not department.is_group:
                frappe.db.set_value(
                    "Department", department.name, "disabled", 1, update_modified=False
                )
    return names


def main() -> None:
    os.chdir(SITES_DIR)
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

        frappe.db.set_value("Company", COMPANY, "tax_id", TAX_ID, update_modified=False)
        address = ensure_company_address()
        branches = ensure_branches()
        departments = ensure_departments()
        set_default_language("es")
        frappe.db.set_single_value("System Settings", "language", "es")
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
                "tax_id": TAX_ID,
                "address": address,
                "branches": branches,
                "departments": departments,
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
