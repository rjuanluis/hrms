const { createRequire } = require("module");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const appRequire = createRequire("/app/package.json");
const { open } = appRequire("lmdb");

const baseUrl = "http://127.0.0.1:3000/api/rpc";
const projectName = "web";
const serviceName = "ayp-hrms";
const sourcePath = process.argv[2];
const mode = process.argv[3] || "--deploy";
const preflightOnly = mode === "--preflight";
const rollbackOnly = mode === "--rollback";
const managedPath = "/etc/easypanel/projects/web/ayp-hrms/code/docker-compose.yml";
const backupRoot = "/etc/easypanel/hermes-backups";
const deployPathPattern = /^\/tmp\/ayp-hrms-compose-[0-9]+\.yml$/;
const rollbackPathPattern =
	/^\/etc\/easypanel\/hermes-backups\/ayp-hrms-compose-pre-[0-9TZ]+\/source-before\.yml$/;

if (
	!sourcePath ||
	(!rollbackOnly && !deployPathPattern.test(sourcePath)) ||
	(rollbackOnly && !rollbackPathPattern.test(sourcePath))
) {
	throw new Error("source path is outside the approved deployment or rollback scope");
}

function decodeRecord(raw) {
	const value = typeof raw === "string" ? JSON.parse(raw) : raw;
	return value && value.json ? value.json : value;
}

function sha256(value) {
	return crypto.createHash("sha256").update(value).digest("hex");
}

function containsExactString(value, target) {
	if (value === target) return true;
	if (Array.isArray(value)) return value.some((item) => containsExactString(item, target));
	if (value && typeof value === "object") {
		return Object.values(value).some((item) => containsExactString(item, target));
	}
	return false;
}

function findInlineSource(value) {
	const matches = new Set();
	function walk(node) {
		if (!node || typeof node !== "object") return;
		if (
			node.source &&
			typeof node.source === "object" &&
			node.source.type === "inline" &&
			typeof node.source.content === "string"
		) {
			matches.add(node.source.content);
		}
		if (node.type === "inline" && typeof node.content === "string") {
			matches.add(node.content);
		}
		for (const child of Object.values(node)) walk(child);
	}
	walk(value);
	const composeSources = [...matches].filter(
		(source) => source.includes("services:") && source.includes("ghcr.io/rjuanluis/ayp-hrms"),
	);
	if (composeSources.length !== 1) {
		throw new Error(`expected one inline Compose source, found ${composeSources.length}`);
	}
	return composeSources[0];
}

async function request(session, endpoint, json) {
	const response = await fetch(`${baseUrl}${endpoint}`, {
		method: "POST",
		headers: { Authorization: session, "Content-Type": "application/json" },
		body: JSON.stringify({ json }),
	});
	const text = await response.text();
	if (!response.ok) throw new Error(`${endpoint} failed with HTTP ${response.status}`);
	try {
		return JSON.parse(text);
	} catch (_) {
		return text;
	}
}

async function findSession() {
	const db = open({ path: "/etc/easypanel/data" });
	const candidates = [];
	for (const { key, value } of db.getRange()) {
		if (!String(key).startsWith("sessions:")) continue;
		try {
			const record = decodeRecord(value);
			if (typeof record?.id === "string" && record.id.length > 20)
				candidates.push(record.id);
		} catch (_) {}
	}
	db.close();

	for (const session of candidates) {
		try {
			await request(session, "/services/compose/inspectService", {
				projectName,
				serviceName,
			});
			return session;
		} catch (_) {}
	}
	return null;
}

async function waitForManagedSource(expected, timeoutMs = 120000) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (fs.existsSync(managedPath) && fs.readFileSync(managedPath, "utf8") === expected)
			return;
		await new Promise((resolve) => setTimeout(resolve, 2000));
	}
	throw new Error("managed EasyPanel Compose file did not converge to requested source");
}

async function applySource(session, source) {
	await request(session, "/services/compose/updateSourceInline", {
		projectName,
		serviceName,
		content: source,
	});
	const updated = await request(session, "/services/compose/inspectService", {
		projectName,
		serviceName,
	});
	if (!containsExactString(updated, source)) {
		throw new Error("EasyPanel canonical source readback does not match requested Compose");
	}
	await request(session, "/services/compose/deployService", {
		projectName,
		serviceName,
		forceRebuild: false,
	});
	await waitForManagedSource(source);
	const deployed = await request(session, "/services/compose/inspectService", {
		projectName,
		serviceName,
	});
	if (!containsExactString(deployed, source)) {
		throw new Error("EasyPanel canonical source changed during deployment");
	}
	return deployed;
}

let backupDir = null;
let mutationMayHaveOccurred = false;

(async () => {
	const source = fs.readFileSync(sourcePath, "utf8");
	if (!rollbackOnly) {
		for (const required of ["configure-workers:", "queue-documents:", "bench", "documents"]) {
			if (!source.includes(required))
				throw new Error(`candidate Compose is missing ${required}`);
		}
	}

	const session = await findSession();
	if (!session) {
		console.error("LOGIN_REQUIRED: no valid EasyPanel admin session");
		process.exit(44);
	}

	if (rollbackOnly) {
		await applySource(session, source);
		console.log(
			JSON.stringify({
				status: "rollback_requested",
				projectName,
				serviceName,
				restoredSha256: sha256(source),
				canonicalSourceMatch: true,
				managedSourceMatch: true,
				externalWrite: true,
			}),
		);
		return;
	}

	const before = await request(session, "/services/compose/inspectService", {
		projectName,
		serviceName,
	});
	if (preflightOnly) {
		console.log(
			JSON.stringify({
				status: "preflight_ok",
				projectName,
				serviceName,
				candidateSha256: sha256(source),
				authenticated: true,
				externalWrite: false,
			}),
		);
		return;
	}

	const previousSource = findInlineSource(before);
	const timestamp = new Date().toISOString().replace(/[-:.]/g, "").replace("Z", "Z");
	backupDir = path.join(backupRoot, `ayp-hrms-compose-pre-${timestamp}`);
	fs.mkdirSync(backupDir, { recursive: true, mode: 0o700 });
	fs.writeFileSync(
		path.join(backupDir, "inspect-service.json"),
		`${JSON.stringify(before, null, 2)}\n`,
		{ mode: 0o600 },
	);
	fs.writeFileSync(path.join(backupDir, "source-before.yml"), previousSource, { mode: 0o600 });
	if (fs.existsSync(managedPath)) {
		fs.copyFileSync(managedPath, path.join(backupDir, "docker-compose.yml"));
		fs.chmodSync(path.join(backupDir, "docker-compose.yml"), 0o600);
	}

	// The update request may commit even if the response is lost, so arm rollback first.
	mutationMayHaveOccurred = true;
	const after = await applySource(session, source);
	fs.writeFileSync(
		path.join(backupDir, "inspect-service-after.json"),
		`${JSON.stringify(after, null, 2)}\n`,
		{ mode: 0o600 },
	);

	console.log(
		JSON.stringify({
			status: "deploy_requested",
			projectName,
			serviceName,
			candidateSha256: sha256(source),
			canonicalSourceMatch: true,
			managedSourceMatch: true,
			externalWrite: true,
			rollbackPath: backupDir,
		}),
	);
})().catch((error) => {
	console.error(error.message);
	console.log(
		JSON.stringify({
			status: "failed",
			rollbackRequired: mutationMayHaveOccurred,
			rollbackPath: backupDir,
			externalWrite: mutationMayHaveOccurred,
		}),
	);
	process.exit(1);
});
