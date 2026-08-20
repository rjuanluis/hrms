from __future__ import annotations

import ast
import os
import threading
import time
import unittest
from pathlib import Path

try:
	import pymysql
except ImportError:  # pragma: no cover - exercised by the CI dependency gate
	pymysql = None

SOURCE = Path(__file__).parents[1] / "hrms" / "recruitment" / "email_bridge.py"
REQUIRED_ENV = (
	"AYP_ATS_TEST_DB_HOST",
	"AYP_ATS_TEST_DB_PORT",
	"AYP_ATS_TEST_DB_NAME",
	"AYP_ATS_TEST_DB_USER",
	"AYP_ATS_TEST_DB_PASSWORD",
)


def _sql_literal_from_source(name: str) -> str:
	tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
	for node in tree.body:
		if isinstance(node, ast.Assign) and any(
			isinstance(target, ast.Name) and target.id == name for target in node.targets
		):
			value = ast.literal_eval(node.value)
			if isinstance(value, str):
				return value
	raise AssertionError(f"{name} is missing or not a literal string")


@unittest.skipUnless(
	pymysql is not None and all(os.environ.get(name) for name in REQUIRED_ENV),
	"isolated MariaDB concurrency environment is required",
)
class TestVacancyAuthorityLockMariaDB(unittest.TestCase):
	def connect(self):
		assert pymysql is not None
		return pymysql.connect(
			host=os.environ["AYP_ATS_TEST_DB_HOST"],
			port=int(os.environ["AYP_ATS_TEST_DB_PORT"]),
			user=os.environ["AYP_ATS_TEST_DB_USER"],
			password=os.environ["AYP_ATS_TEST_DB_PASSWORD"],
			database=os.environ["AYP_ATS_TEST_DB_NAME"],
			charset="utf8mb4",
			autocommit=False,
		)

	def setUp(self):
		self.admin = self.connect()
		self.addCleanup(self._cleanup_table)
		with self.admin.cursor() as cursor:
			cursor.execute("DROP TABLE IF EXISTS `tabJob Opening`")
			cursor.execute("DROP TABLE IF EXISTS `tabFile`")
			cursor.execute("DROP TABLE IF EXISTS `tabJob Applicant`")
			cursor.execute(
				"""
				CREATE TABLE `tabJob Opening` (
					`name` varchar(140) NOT NULL,
					`status` varchar(32) NOT NULL,
					PRIMARY KEY (`name`)
				) ENGINE=InnoDB
				"""
			)
			cursor.execute(
				"INSERT INTO `tabJob Opening` (`name`, `status`) VALUES (%s, %s), (%s, %s)",
				("HR-OPN-2026-0001", "Open", "HR-OPN-2026-0002", "Closed"),
			)
			cursor.execute("CREATE TABLE `tabFile` (`name` varchar(140) PRIMARY KEY) ENGINE=InnoDB")
			cursor.execute("CREATE TABLE `tabJob Applicant` (`name` varchar(140) PRIMARY KEY) ENGINE=InnoDB")
		self.admin.commit()

	def _cleanup_table(self):
		try:
			with self.admin.cursor() as cursor:
				cursor.execute("DROP TABLE IF EXISTS `tabJob Opening`")
				cursor.execute("DROP TABLE IF EXISTS `tabFile`")
				cursor.execute("DROP TABLE IF EXISTS `tabJob Applicant`")
			self.admin.commit()
		finally:
			self.admin.close()

	def assert_mutation_waits_for_authority_lock(self, sql: str, params: tuple[str, ...]):
		lock_connection = self.connect()
		started = threading.Event()
		completed = threading.Event()
		release = threading.Event()
		errors: list[BaseException] = []

		def mutate():
			connection = self.connect()
			try:
				with connection.cursor() as cursor:
					cursor.execute("SET SESSION innodb_lock_wait_timeout = 10")
					started.set()
					cursor.execute(sql, params)
				completed.set()
				release.wait(5)
				connection.rollback()
			except BaseException as exc:  # Preserve the exact worker failure for the test thread.
				errors.append(exc)
				completed.set()
			finally:
				connection.close()

		worker = threading.Thread(target=mutate, daemon=True)
		try:
			with lock_connection.cursor() as cursor:
				cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
				lock_connection.begin()
				cursor.execute(_sql_literal_from_source("TRANSACTION_ISOLATION_SQL"))
				isolation_row = cursor.fetchone()
				self.assertIsNotNone(isolation_row)
				assert isolation_row is not None
				self.assertEqual(isolation_row[0].replace("_", "-").upper(), "REPEATABLE-READ")
				cursor.execute(_sql_literal_from_source("JOB_OPENING_LOCK_SQL"))
				self.assertEqual(
					cursor.fetchall(),
					(("HR-OPN-2026-0001", "Open"), ("HR-OPN-2026-0002", "Closed")),
				)
			worker.start()
			self.assertTrue(started.wait(2), "concurrent mutation did not start")
			time.sleep(0.75)
			self.assertFalse(completed.is_set(), "mutation crossed the vacancy authority lock")
			with lock_connection.cursor() as cursor:
				cursor.execute("INSERT INTO `tabFile` (`name`) VALUES (%s)", ("FILE-CANDIDATE",))
				cursor.execute(
					"INSERT INTO `tabJob Applicant` (`name`) VALUES (%s)", ("APPLICANT-CANDIDATE",)
				)
			self.assertFalse(
				completed.is_set(), "mutation crossed the lock during file and applicant insertion"
			)
			lock_connection.commit()
			self.assertTrue(completed.wait(5), "mutation did not resume after authority commit")
			if errors:
				raise errors[0]
		finally:
			release.set()
			worker.join(5)
			lock_connection.rollback()
			lock_connection.close()

		verification = self.connect()
		try:
			with verification.cursor() as cursor:
				cursor.execute("SELECT `name`, `status` FROM `tabJob Opening` ORDER BY `name`")
				self.assertEqual(
					cursor.fetchall(),
					(("HR-OPN-2026-0001", "Open"), ("HR-OPN-2026-0002", "Closed")),
				)
				cursor.execute("SELECT COUNT(*) FROM `tabFile`")
				file_count = cursor.fetchone()
				self.assertIsNotNone(file_count)
				assert file_count is not None
				self.assertEqual(file_count[0], 1)
				cursor.execute("SELECT COUNT(*) FROM `tabJob Applicant`")
				applicant_count = cursor.fetchone()
				self.assertIsNotNone(applicant_count)
				assert applicant_count is not None
				self.assertEqual(applicant_count[0], 1)
		finally:
			verification.close()

	def test_insert_of_second_vacancy_waits_until_authority_transaction_finishes(self):
		self.assert_mutation_waits_for_authority_lock(
			"INSERT INTO `tabJob Opening` (`name`, `status`) VALUES (%s, %s)",
			("HR-OPN-2026-0003", "Open"),
		)

	def test_status_transition_waits_until_authority_transaction_finishes(self):
		self.assert_mutation_waits_for_authority_lock(
			"UPDATE `tabJob Opening` SET `status` = %s WHERE `name` = %s",
			("Open", "HR-OPN-2026-0002"),
		)


if __name__ == "__main__":
	unittest.main()
