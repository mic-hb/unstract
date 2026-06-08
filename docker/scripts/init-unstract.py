#!/usr/bin/env python3
"""One-shot init for Dokploy deployments.

Replicates the host-side work that `run-platform.sh` does in `setup_env()`:
- copies each service's sample.env to .env (backend, platform-service,
  prompt-service, x2text-service, runner, workers)
- injects a fresh Fernet ENCRYPTION_KEY into backend/.env and
  platform-service/.env
- injects DEFAULT_AUTH_USERNAME / DEFAULT_AUTH_PASSWORD into backend/.env
- copies docker/sample.essentials.env to docker/essentials.env
- copies docker/sample.env to docker/.env, with TOOL_REGISTRY_CONFIG_SRC_PATH
  rewritten to a compose-relative path
- copies docker/sample.proxy_overrides.yaml to docker/proxy_overrides.yaml
  (required for the reverse-proxy bind mount)

Existing .env files are NEVER overwritten, so the ENCRYPTION_KEY persists
across redeploys. To force regeneration, delete the relevant .env file on
the host and redeploy.

Runs as root in the init container, which has the repo root bind-mounted at
/unstract. Writes therefore land on the host filesystem and are picked up by
the other services via their env_file: directives.
"""
from __future__ import annotations

import base64
import secrets
import shutil
import sys
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else "/unstract")
DOCKER = REPO / "docker"

SERVICES_WITH_SAMPLE_ENV = [
    "backend",
    "platform-service",
    "prompt-service",
    "x2text-service",
    "runner",
    "workers",
]

SERVICES_NEEDING_FERNET_KEY = {"backend", "platform-service"}
SERVICE_NEEDING_DEFAULT_ADMIN = "backend"

DEFAULT_AUTH_USERNAME = "unstract"
DEFAULT_AUTH_PASSWORD = "unstract"


def generate_fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def upsert_env_line(text: str, line: str) -> str:
    """Replace KEY=... with `line` (if present), else append."""
    key = line.split("=", 1)[0]
    out: list[str] = []
    replaced = False
    for existing in text.splitlines():
        stripped = existing.lstrip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            out.append(line)
            replaced = True
        else:
            out.append(existing)
    if not replaced:
        out.append(line)
    return "\n".join(out) + "\n"


def ensure_service_env(service: str) -> None:
    svc_dir = REPO / service
    sample = svc_dir / "sample.env"
    target = svc_dir / ".env"

    if target.exists():
        print(f"[skip] {target.relative_to(REPO)} already exists")
        return
    if not sample.exists():
        print(f"[skip] no sample.env in {service}")
        return

    shutil.copyfile(sample, target)
    text = target.read_text()
    mutated = False
    if service in SERVICES_NEEDING_FERNET_KEY:
        text = upsert_env_line(text, f'ENCRYPTION_KEY="{generate_fernet_key()}"')
        mutated = True
    if service == SERVICE_NEEDING_DEFAULT_ADMIN:
        text = upsert_env_line(text, f'DEFAULT_AUTH_USERNAME="{DEFAULT_AUTH_USERNAME}"')
        text = upsert_env_line(text, f'DEFAULT_AUTH_PASSWORD="{DEFAULT_AUTH_PASSWORD}"')
        mutated = True
    if mutated:
        target.write_text(text)
    suffix = " (injected secrets)" if mutated else ""
    print(f"[ok]   {target.relative_to(REPO)}{suffix}")


def ensure_simple_copy(
    sample: Path, target: Path, transform=None
) -> None:
    if target.exists():
        print(f"[skip] {target.relative_to(REPO)} already exists")
        return
    if transform is None:
        shutil.copyfile(sample, target)
    else:
        target.write_text(transform(sample.read_text()))
    print(f"[ok]   {target.relative_to(REPO)}")


def main() -> int:
    for service in SERVICES_WITH_SAMPLE_ENV:
        ensure_service_env(service)

    ensure_simple_copy(DOCKER / "sample.essentials.env", DOCKER / "essentials.env")

    def fix_docker_env(text: str) -> str:
        # sample.env has TOOL_REGISTRY_CONFIG_SRC_PATH="${PWD}/..." which
        # is a shell expansion that doesn't happen in a compose .env file.
        # Rewrite to a compose-relative path so it works regardless of
        # where the repo is cloned.
        return upsert_env_line(
            text,
            "TOOL_REGISTRY_CONFIG_SRC_PATH="
            "./../unstract/tool-registry/tool_registry_config",
        )

    ensure_simple_copy(DOCKER / "sample.env", DOCKER / ".env", transform=fix_docker_env)

    # proxy_overrides.yaml is bind-mounted into the reverse-proxy container;
    # the compose.dokploy.yaml override removes the Traefik file provider,
    # so the contents are ignored, but the file must exist on the host.
    ensure_simple_copy(
        DOCKER / "sample.proxy_overrides.yaml", DOCKER / "proxy_overrides.yaml"
    )

    backend_env = REPO / "backend" / ".env"
    if backend_env.exists():
        for line in backend_env.read_text().splitlines():
            if line.startswith("ENCRYPTION_KEY="):
                print("")
                print("=" * 78)
                print("  ENCRYPTION_KEY (just generated):")
                print(f"    {line}")
                print("  >>> Back this up. Loss = all stored adapter credentials")
                print("  >>> become inaccessible. Already-existing .env files were")
                print("  >>> left untouched, so this key is fresh only on a clean")
                print("  >>> first deploy.")
                print("=" * 78)
                break

    print("\nSetup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
