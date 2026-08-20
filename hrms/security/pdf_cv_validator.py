from __future__ import annotations

import io
import os
import resource
import sys

MAX_PDF_OBJECTS = 5000
MAX_PDF_GRAPH_NODES = 20000
MAX_ADDRESS_SPACE_BYTES = 192 * 1024 * 1024
MAX_CPU_SECONDS = 4
SELF_TEST_MEMORY_LIMIT_ENFORCED = 86
SELF_TEST_LIMIT_SETUP_FAILED = 87
SELF_TEST_PARSER_PRELOADED = 88
SELF_TEST_PARSER_IMPORT_FAILED = 89
PARSER_RUNTIME_FAILED = 90

# These are intentionally loaded only after the child process has installed
# its hard limits. Importing pypdf before RLIMIT_AS leaves parser import and
# initialization outside the confinement boundary.
PdfReader = None
ArrayObject = None
DictionaryObject = None
IndirectObject = None
NameObject = None
PARSER_CONTENT_ERRORS: tuple[type[Exception], ...] = ()

FORBIDDEN_KEYS = {
	"/AA",
	"/AcroForm",
	"/OpenAction",
	"/XFA",
	"/EF",
	"/AF",
}
FORBIDDEN_ACTIONS = {
	"/JavaScript",
	"/Launch",
	"/SubmitForm",
	"/ImportData",
	"/GoToR",
	"/GoToE",
	"/Rendition",
	"/Movie",
	"/Sound",
}
FORBIDDEN_SUBTYPES = {
	"/FileAttachment",
	"/RichMedia",
	"/3D",
	"/Movie",
	"/Sound",
	"/Screen",
}


class PDFSecurityError(ValueError):
	pass


def _strict_name_unnumber(raw: bytes) -> bytes:
	result = bytearray()
	index = 0
	while index < len(raw):
		if raw[index : index + 1] != b"#":
			result.append(raw[index])
			index += 1
			continue
		if index + 2 >= len(raw):
			raise PDFSecurityError("malformed-name-escape")
		try:
			result.append(int(raw[index + 1 : index + 3], 16))
		except ValueError as exc:
			raise PDFSecurityError("malformed-name-escape") from exc
		index += 3
	return bytes(result)


def _set_memory_limit(limit) -> bool:
	try:
		_soft, hard = resource.getrlimit(limit)
		target = MAX_ADDRESS_SPACE_BYTES
		if hard != resource.RLIM_INFINITY:
			target = min(target, hard)
		if target <= 0:
			return False
		resource.setrlimit(limit, (target, target))
		effective_soft, _ = resource.getrlimit(limit)
		return effective_soft != resource.RLIM_INFINITY and 0 < effective_soft <= MAX_ADDRESS_SPACE_BYTES
	except (OSError, ValueError):
		return False


def _set_limits() -> None:
	memory_limit_active = False
	for limit_name in ("RLIMIT_AS", "RLIMIT_DATA"):
		limit = getattr(resource, limit_name, None)
		if limit is not None:
			memory_limit_active = _set_memory_limit(limit) or memory_limit_active
	if not memory_limit_active:
		raise PDFSecurityError("memory-limit-unavailable")
	resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS + 1))
	resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
	for limit_name, maximum in (("RLIMIT_NOFILE", 16), ("RLIMIT_NPROC", 0), ("RLIMIT_CORE", 0)):
		limit = getattr(resource, limit_name, None)
		if limit is None:
			continue
		try:
			resource.setrlimit(limit, (maximum, maximum))
		except (OSError, ValueError):
			pass


def _load_parser() -> None:
	global PdfReader, ArrayObject, DictionaryObject, IndirectObject, NameObject, PARSER_CONTENT_ERRORS
	from pypdf import PdfReader as _PdfReader
	from pypdf.errors import PdfReadError as _PdfReadError
	from pypdf.generic import (
		ArrayObject as _ArrayObject,
	)
	from pypdf.generic import (
		DictionaryObject as _DictionaryObject,
	)
	from pypdf.generic import (
		IndirectObject as _IndirectObject,
	)
	from pypdf.generic import (
		NameObject as _NameObject,
	)

	PdfReader = _PdfReader
	ArrayObject = _ArrayObject
	DictionaryObject = _DictionaryObject
	IndirectObject = _IndirectObject
	NameObject = _NameObject
	PARSER_CONTENT_ERRORS = (_PdfReadError,)


def _name(value) -> str:
	if not isinstance(value, NameObject):
		return ""
	name = str(value)
	payload = name[1:] if name.startswith("/") else name
	index = 0
	while index < len(payload):
		if payload[index] != "#":
			index += 1
			continue
		if index + 2 >= len(payload) or any(
			character not in "0123456789abcdefABCDEF" for character in payload[index + 1 : index + 3]
		):
			raise PDFSecurityError("malformed-name-escape")
		index += 3
	return name


def _validate_dictionary(value: DictionaryObject) -> None:
	keys = {_name(key) for key in value.keys()}
	if keys & FORBIDDEN_KEYS:
		raise PDFSecurityError("active-key")
	if "/EmbeddedFiles" in keys:
		raise PDFSecurityError("embedded-files")
	if _name(value.get("/S")) in FORBIDDEN_ACTIONS:
		raise PDFSecurityError("active-action")
	if _name(value.get("/Subtype")) in FORBIDDEN_SUBTYPES:
		raise PDFSecurityError("active-annotation")


def _object_refs(reader: PdfReader) -> list[IndirectObject]:
	refs = [
		IndirectObject(object_id, generation, reader)
		for generation, objects in reader.xref.items()
		for object_id in objects
	]
	refs.extend(IndirectObject(object_id, 0, reader) for object_id in reader.xref_objStm)
	if len(refs) > MAX_PDF_OBJECTS:
		raise PDFSecurityError("too-many-objects")
	return refs


def validate_pdf_bytes(content: bytes) -> None:
	# pypdf preserves malformed # escapes by default. Inside this confined
	# process, make its actual Name parser fail closed before graph traversal.
	NameObject.unnumber = staticmethod(_strict_name_unnumber)
	reader = PdfReader(io.BytesIO(content), strict=True)
	if reader.is_encrypted:
		raise PDFSecurityError("encrypted")
	seen_indirect: set[tuple[int, int]] = set()
	seen_direct: set[int] = set()
	stack: list = [reader.trailer, *_object_refs(reader)]
	nodes = 0
	while stack:
		value = stack.pop()
		nodes += 1
		if nodes > MAX_PDF_GRAPH_NODES:
			raise PDFSecurityError("too-complex")
		if isinstance(value, IndirectObject):
			identity = (value.generation, value.idnum)
			if identity in seen_indirect:
				continue
			seen_indirect.add(identity)
			resolved = reader.get_object(value)
			if resolved is None:
				raise PDFSecurityError("invalid-reference")
			stack.append(resolved)
			continue
		if isinstance(value, DictionaryObject):
			identity = id(value)
			if identity in seen_direct:
				continue
			seen_direct.add(identity)
			_validate_dictionary(value)
			for key, child in value.items():
				stack.extend((key, child))
			continue
		if isinstance(value, ArrayObject):
			identity = id(value)
			if identity in seen_direct:
				continue
			seen_direct.add(identity)
			stack.extend(value)
	if not reader.pages:
		raise PDFSecurityError("no-pages")


def _parser_content_error_is_deterministic(exc: Exception) -> bool:
	current = exc.__cause__ or exc.__context__
	while current is not None:
		if not isinstance(current, (PDFSecurityError, *PARSER_CONTENT_ERRORS)):
			return False
		current = current.__cause__ or current.__context__
	return True


def main() -> int:
	parser_was_preloaded = any(name == "pypdf" or name.startswith("pypdf.") for name in sys.modules)
	try:
		_set_limits()
	except Exception:
		return SELF_TEST_LIMIT_SETUP_FAILED
	self_test = os.environ.get("AYP_PDF_VALIDATOR_SELF_TEST") == "memory"
	if self_test and parser_was_preloaded:
		return SELF_TEST_PARSER_PRELOADED
	try:
		_load_parser()
	except Exception:
		return SELF_TEST_PARSER_IMPORT_FAILED
	# The parent validator passes a minimal environment containing only PATH,
	# so uploaded bytes cannot activate this test-only confinement probe.
	# Importing pypdf before the verified limit, failing to import it, or failing
	# to receive MemoryError each has a distinct result and cannot fake success.
	if self_test:
		if "pypdf" not in sys.modules or PdfReader is None:
			return SELF_TEST_PARSER_IMPORT_FAILED
		blocks = []
		try:
			while True:
				blocks.append(bytearray(16 * 1024 * 1024))
		except MemoryError:
			return SELF_TEST_MEMORY_LIMIT_ENFORCED
	content = sys.stdin.buffer.read()
	try:
		validate_pdf_bytes(content)
	except PDFSecurityError:
		return 2
	except PARSER_CONTENT_ERRORS as exc:
		# pypdf's documented read-error family represents deterministic malformed
		# content unless it wrapped a resource or unknown runtime exception.
		# Strict-mode pypdf may wrap arbitrary exceptions as PdfReadError while
		# preserving the original in the exception chain.
		return 2 if _parser_content_error_is_deterministic(exc) else PARSER_RUNTIME_FAILED
	except Exception:
		# Unknown parser/runtime failures, including MemoryError, are
		# infrastructure faults. Only our explicit PDFSecurityError contract is
		# a deterministic content rejection that the parent may persist.
		return PARSER_RUNTIME_FAILED
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
