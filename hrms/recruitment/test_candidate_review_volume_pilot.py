from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative_path: str):
	spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
	if spec is None or spec.loader is None:
		raise RuntimeError(f"No se pudo cargar {relative_path}")
	module = importlib.util.module_from_spec(spec)
	sys.modules[name] = module
	spec.loader.exec_module(module)
	return module


def run_volume_pilot(candidate_count: int = 250) -> dict:
	review = load_module("ayp_pilot_review", "hrms/recruitment/candidate_review_domain.py")
	scoring = load_module("ayp_pilot_scoring", "hrms/recruitment/candidate_scoring_domain.py")
	started = time.perf_counter()

	candidates = [f"HR-APP-{index:04d}" for index in range(1, candidate_count + 1)]
	pages = [candidates[start : start + 50] for start in range(0, candidate_count, 50)]
	batches = [candidates[start : start + 100] for start in range(0, candidate_count, 100)]
	for page_start, page in enumerate(pages):
		filters = review.ReviewFilters.from_input(
			{"job_title": "JOB-AYP-PILOT", "sort_by": "score"},
			start=page_start * 50,
			page_length=50,
		)
		assert filters.start == page_start * 50 and filters.page_length == len(page)
	for batch in batches:
		request = review.BatchReviewRequest.from_input(
			batch,
			target_status="Shortlisted",
			reason="Piloto sintético de volumen con decisión humana pendiente.",
			job_title="JOB-AYP-PILOT",
		)
		assert len(request.applicant_names) <= review.MAX_BATCH_SIZE

	criteria = [
		{
			"criterion_key": criterion.key,
			"rating": 4,
			"evidence": f"Evidencia sintética reproducible para {criterion.label}.",
		}
		for criterion in scoring.DEFAULT_CRITERIA
	]
	scorecards = [scoring.Scorecard.from_input(criteria) for _candidate in candidates]
	assert all(card.total_score == 80 for card in scorecards)
	assert all(card.recommendation == "Recomendado para shortlist" for card in scorecards)

	interviews = {}
	for index, candidate in enumerate(candidates[:100], start=1):
		assigned = {"one@example.com", "two@example.com"}
		submitted = {"one@example.com"} if index % 3 else assigned
		results = {"Cleared", "Rejected"} if index % 10 == 0 else {"Cleared"}
		interviews[candidate] = {
			"assigned": len(assigned),
			"submitted": len(submitted),
			"missing": len(assigned - submitted),
			"disagreement": len(results) > 1,
		}
	assert sum(row["missing"] for row in interviews.values()) == 67
	assert sum(1 for row in interviews.values() if row["disagreement"]) == 10

	profile_actions = {}
	for index, candidate in enumerate(candidates, start=1):
		profile_actions[candidate] = "Prioritario" if index <= 50 else "Activo"
	dedupe_resolutions = {
		candidate: "Manual" for index, candidate in enumerate(candidates, start=1) if index % 10 == 0
	}
	assert len(profile_actions) == candidate_count
	assert sum(status == "Prioritario" for status in profile_actions.values()) == 50
	assert len(dedupe_resolutions) == 25
	assert "Eliminación solicitada" not in set(profile_actions.values())

	return {
		"volume_pilot": True,
		"candidates": candidate_count,
		"pages": len(pages),
		"batch_sizes": [len(batch) for batch in batches],
		"scorecards": len(scorecards),
		"interviews": len(interviews),
		"missing_feedback": sum(row["missing"] for row in interviews.values()),
		"disagreements": sum(1 for row in interviews.values() if row["disagreement"]),
		"talent_pool_decisions": len(profile_actions),
		"priority_profiles": sum(status == "Prioritario" for status in profile_actions.values()),
		"manual_dedupe_resolutions": len(dedupe_resolutions),
		"elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
	}


if __name__ == "__main__":
	print(json.dumps(run_volume_pilot(), sort_keys=True))
