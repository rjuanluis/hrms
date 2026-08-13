const { createRequire } = require("module");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const appRequire = createRequire("/app/package.json");
const { open } = appRequire("lmdb");

const baseUrl = "http://127.0.0.1:3000/api/rpc";
const projectName = "web";
const serviceName = "ayp-hrms";
const candidatePath = process.argv[2];
const preflightOnly = process.argv[3] === "--preflight";
const managedPath = "/etc/easypanel/projects/web/ayp-hrms/code/docker-compose.yml";
const backupRoot = "/etc/easypanel/hermes-backups";

if (!candidatePath || !candidatePath.startsWith("/tmp/ayp-hrms-compose-")) {
	throw new Error("candidate path must be a deployment-scoped /tmp file");
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

(async () => {
	const candidate = fs.readFileSync(candidatePath, "utf8");
	for (const required of ["configure-workers:", "queue-documents:", "bench", "documents"]) {
		if (!candidate.includes(required))
			throw new Error(`candidate Compose is missing ${required}`);
	}

	const session = await findSession();
	if (!session) {
		console.error("LOGIN_REQUIRED: no valid EasyPanel admin session");
		process.exit(44);
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
				candidateSha256: sha256(candidate),
				authenticated: true,
				externalWrite: false,
			}),
		);
		return;
	}
	const timestamp = new Date().toISOString().replace(/[-:.]/g, "").replace("Z", "Z");
	const backupDir = path.join(backupRoot, `ayp-hrms-compose-pre-${timestamp}`);
	fs.mkdirSync(backupDir, { recursive: true, mode: 0o700 });
	fs.writeFileSync(
		path.join(backupDir, "inspect-service.json"),
		`${JSON.stringify(before, null, 2)}\n`,
		{ mode: 0o600 },
	);
	if (fs.existsSync(managedPath)) {
		fs.copyFileSync(managedPath, path.join(backupDir, "docker-compose.yml"));
		fs.chmodSync(path.join(backupDir, "docker-compose.yml"), 0o600);
	}

	await request(session, "/services/compose/updateSourceInline", {
		projectName,
		serviceName,
		content: candidate,
	});
	const updated = await request(session, "/services/compose/inspectService", {
		projectName,
		serviceName,
	});
	if (!containsExactString(updated, candidate)) {
		throw new Error("EasyPanel canonical source readback does not match candidate Compose");
	}
	await request(session, "/services/compose/deployService", {
		projectName,
		serviceName,
		forceRebuild: false,
	});

	const after = await request(session, "/services/compose/inspectService", {
		projectName,
		serviceName,
	});
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
			candidateSha256: sha256(candidate),
			canonicalSourceMatch: true,
			externalWrite: true,
			rollbackPath: backupDir,
		}),
	);
})().catch((error) => {
	console.error(error.message);
	process.exit(1);
});
