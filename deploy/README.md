# AyP HR official deployment

This directory defines the official `hr.aroypedal.com` deployment on the Hostinger VPS. It is not a disposable sandbox.

## Source of truth

- Repository: `rjuanluis/hrms`
- Production branch: `ayp-production`
- Base: upstream `frappe/hrms` `version-16`
- Container registry: `ghcr.io/rjuanluis/ayp-hrms`
- Runtime: Docker Swarm on the existing Hostinger VPS
- Public route: existing EasyPanel Traefik and Cloudflare DNS

Server state is mutable, but every infrastructure manifest, bootstrap routine, migration and deployment command must be represented here. Secrets must never be committed.

## Deployment flow

1. A push to `ayp-production` validates the source and builds an immutable image.
2. GitHub Actions connects using a restricted deployment key.
3. `host-deploy-wrapper.sh` permits only an AyP HR image deployment.
4. `deploy.sh` updates the stack, creates or migrates the site, takes a pre-migration backup and forces application services to reload.
5. Traefik serves the site at `https://hr.aroypedal.com`.

## One-time host provisioning

Run as root on the official Hostinger VPS only:

```bash
AYP_DEPLOY_PUBLIC_KEY_FILE=/root/ayp-hr-deploy.pub \
  /path/to/hrms/deploy/provision-host.sh
```

The provisioner:

- creates the restricted `aypdeploy` account;
- creates persistent volumes and the private overlay network;
- generates database and Administrator secrets under `/opt/ayp-hr/secrets` with restricted permissions;
- creates a 4 GiB swap file when absent;
- registers the isolated Traefik route;
- installs the six-hour local backup schedule.

It does not print secret values.

## Secret retrieval

The initial Frappe Administrator credential is stored only at:

```text
/opt/ayp-hr/secrets/admin_password
```

It must be retrieved over an approved SSH/Hostinger console path, changed immediately after the first successful login, and then removed from the host after recovery access is verified.

## Backup posture

`backup.sh` runs every six hours and retains fourteen days of local backup artifacts. Local backups protect against application mistakes, not total VPS loss. Offsite encrypted storage must be configured before real employee data is imported.

## Update policy

Do not merge upstream `develop` into production. Update from upstream `version-16` in a PR, run CI, inspect migrations, make a fresh backup, and deploy the immutable commit image.
