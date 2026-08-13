#!/usr/bin/env python3
"""Reconcile Frappe v16 bootstrap DDL with its shipped Meta DocTypes.

Frappe v16.27.1 ships framework_mariadb.sql older than the JSON metadata in
that same release. Fresh-site inserts therefore fail before ERPNext/HRMS can
install. Definitions below are the resulting MariaDB definitions read from a
migrated v16.27.1 site. The patch is build-time, idempotent, and fail-closed.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

TABLE_DEFINITIONS = {
	"tabDocField": (
		"`non_negative` tinyint NOT NULL DEFAULT 0,",
		"`is_virtual` tinyint NOT NULL DEFAULT 0,",
		"`not_nullable` tinyint NOT NULL DEFAULT 0,",
		"`mask` tinyint NOT NULL DEFAULT 0,",
		"`sort_options` tinyint NOT NULL DEFAULT 0,",
		"`link_filters` longtext DEFAULT NULL,",
		"`fetch_from` text DEFAULT NULL,",
		"`button_color` varchar(140) DEFAULT NULL,",
		"`show_on_timeline` tinyint NOT NULL DEFAULT 0,",
		"`sticky` tinyint NOT NULL DEFAULT 0,",
		"`make_attachment_public` tinyint NOT NULL DEFAULT 0,",
		"`alignment` varchar(140) DEFAULT NULL,",
		"`documentation_url` varchar(140) DEFAULT NULL,",
		"`placeholder` varchar(140) DEFAULT NULL,",
		"`show_description_on_click` tinyint NOT NULL DEFAULT 0,",
	),
	"tabDocPerm": (
		"`if_owner` tinyint NOT NULL DEFAULT 0,",
		"`select` tinyint NOT NULL DEFAULT 0,",
		"`mask` tinyint NOT NULL DEFAULT 0,",
	),
	"tabDocType Action": (
		"`hidden` tinyint NOT NULL DEFAULT 0,",
		"`custom` tinyint NOT NULL DEFAULT 0,",
	),
	"tabDocType Link": (
		"`parent_doctype` varchar(140) DEFAULT NULL,",
		"`table_fieldname` varchar(140) DEFAULT NULL,",
		"`hidden` tinyint NOT NULL DEFAULT 0,",
		"`is_child_table` tinyint NOT NULL DEFAULT 0,",
		"`custom` tinyint NOT NULL DEFAULT 0,",
	),
	"tabDocType": (
		"`protect_attached_files` tinyint NOT NULL DEFAULT 0,",
		"`is_calendar_and_gantt` tinyint NOT NULL DEFAULT 0,",
		"`quick_entry` tinyint NOT NULL DEFAULT 0,",
		"`grid_page_length` int NOT NULL DEFAULT 50,",
		"`rows_threshold_for_grid_search` int NOT NULL DEFAULT 20,",
		"`allow_bulk_edit` tinyint NOT NULL DEFAULT 1,",
		"`track_views` tinyint NOT NULL DEFAULT 0,",
		"`queue_in_background` tinyint NOT NULL DEFAULT 0,",
		"`nsm_parent_field` varchar(140) DEFAULT NULL,",
		"`documentation` varchar(140) DEFAULT NULL,",
		"`allow_events_in_timeline` tinyint NOT NULL DEFAULT 0,",
		"`allow_auto_repeat` tinyint NOT NULL DEFAULT 0,",
		"`make_attachments_public` tinyint NOT NULL DEFAULT 0,",
		"`default_view` varchar(140) DEFAULT NULL,",
		"`force_re_route_to_default_view` tinyint NOT NULL DEFAULT 0,",
		"`show_preview_popup` tinyint NOT NULL DEFAULT 0,",
		"`default_email_template` varchar(140) DEFAULT NULL,",
		"`sender_name_field` varchar(140) DEFAULT NULL,",
		"`recipient_account_field` varchar(140) DEFAULT NULL,",
		"`index_web_pages_for_search` tinyint NOT NULL DEFAULT 1,",
		"`row_format` varchar(140) DEFAULT 'Dynamic',",
	),
	"tabFile": (
		"`is_private` tinyint NOT NULL DEFAULT 0,",
		"`file_type` varchar(140) DEFAULT NULL,",
		"`is_home_folder` tinyint NOT NULL DEFAULT 0,",
		"`is_attachments_folder` tinyint NOT NULL DEFAULT 0,",
		"`thumbnail_url` text DEFAULT NULL,",
		"`folder` varchar(255) DEFAULT NULL,",
		"`is_folder` tinyint NOT NULL DEFAULT 0,",
		"`attached_to_field` varchar(140) DEFAULT NULL,",
		"`old_parent` varchar(140) DEFAULT NULL,",
		"`content_hash` varchar(140) DEFAULT NULL,",
		"`uploaded_to_dropbox` tinyint NOT NULL DEFAULT 0,",
		"`uploaded_to_google_drive` tinyint NOT NULL DEFAULT 0,",
	),
}


def _table_block(text: str, table: str) -> tuple[int, int, str]:
	marker = f"CREATE TABLE `{table}`"
	if text.count(marker) != 1:
		raise RuntimeError(f"unexpected bootstrap schema: {table} block is not unique")
	start = text.index(marker)
	end = text.find(";", start)
	if end < 0:
		raise RuntimeError(f"unexpected bootstrap schema: {table} block is unterminated")
	return start, end + 1, text[start : end + 1]


def patch_schema(path: Path) -> dict[str, list[str]]:
	text = path.read_text(encoding="utf-8")
	added: dict[str, list[str]] = {}
	for table, definitions in TABLE_DEFINITIONS.items():
		start, end, block = _table_block(text, table)
		existing = set(re.findall(r"^  `([^`]+)`", block, re.MULTILINE))
		missing = [definition for definition in definitions if definition.split("`", 2)[1] not in existing]
		if missing:
			primary = re.search(r"^  PRIMARY KEY ", block, re.MULTILINE)
			if not primary:
				raise RuntimeError(f"unexpected bootstrap schema: {table} has no primary-key anchor")
			insertion = "\n".join(f"  {definition}" for definition in missing) + "\n"
			block = block[: primary.start()] + insertion + block[primary.start() :]
			text = text[:start] + block + text[end:]
			added[table] = [definition.split("`", 2)[1] for definition in missing]
	path.write_text(text, encoding="utf-8")

	final = path.read_text(encoding="utf-8")
	for table, definitions in TABLE_DEFINITIONS.items():
		_, _, block = _table_block(final, table)
		for definition in definitions:
			name = definition.split("`", 2)[1]
			if len(re.findall(rf"^  `{re.escape(name)}`\s", block, re.MULTILINE)) != 1:
				raise RuntimeError(f"bootstrap reconciliation failed: {table}.{name}")
	return added


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument("schema", type=Path)
	args = parser.parse_args()
	added = patch_schema(args.schema)
	print(f"frappe_bootstrap_tables_patched={len(added)}")
	print(f"frappe_bootstrap_columns_added={sum(map(len, added.values()))}")


if __name__ == "__main__":
	main()
