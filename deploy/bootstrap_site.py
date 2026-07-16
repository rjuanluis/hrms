#!/usr/bin/env python3
"""Create the official AyP Frappe site without placing secrets in argv or Git."""
from __future__ import annotations

import os
from pathlib import Path

from frappe.commands.site import new_site

SITE = os.environ.get("AYP_SITE_NAME", "hr.aroypedal.com")
SITES_DIR = Path("/home/frappe/frappe-bench/sites")
SECRETS_DIR = Path(os.environ.get("AYP_SECRETS_DIR", "/run/ayp-secrets"))


def read_secret(name: str) -> str:
    env_name = f"AYP_{name.upper()}"
    value = os.environ.get(env_name, "").strip()
    if not value and (SECRETS_DIR / name).is_file():
        value = (SECRETS_DIR / name).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Required secret {name} is empty")
    return value


def main() -> None:
    if (SITES_DIR / SITE / "site_config.json").exists():
        print(f"Site {SITE} already exists; bootstrap skipped")
        return

    db_root_password = read_secret("db_root_password")
    admin_password = read_secret("admin_password")
    os.chdir(SITES_DIR)

    new_site.callback(
        site=SITE,
        db_root_username="root",
        db_root_password=db_root_password,
        admin_password=admin_password,
        verbose=False,
        source_sql=None,
        force=False,
        no_mariadb_socket=False,
        mariadb_user_host_login_scope="%",
        install_app=("erpnext", "hrms"),
        db_name=None,
        db_password=None,
        db_type="mariadb",
        db_socket=None,
        db_host="db",
        db_port=3306,
        db_user=None,
        set_default=True,
        setup_db=True,
    )
    print(f"Created {SITE} with ERPNext and HRMS")


if __name__ == "__main__":
    main()
