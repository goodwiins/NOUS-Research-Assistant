"""Scan and upload adapted K-Dense project skills.

Offline scan (default): runs the real server-side parser + scanner against every
adapted SKILL.md and reports blockers/warnings without touching a server.

Upload (--upload): POSTs each clean document to the project-skills API as a
pending change request for a reviewer to approve. Requires the catalog
feature flag to be enabled server-side and a bearer token with access to the
project, read from $NOUS_TOKEN (never from argv, where it would show up in
`ps` and shell history).

Approving your own requests skips review, so it needs --self-approve, and
acknowledging scanner warnings needs --acknowledge-warnings.

Usage:
    python scripts/project_skills/import_kdense.py                # scan only
    NOUS_TOKEN=... python scripts/project_skills/import_kdense.py --upload \
        --base-url http://localhost:8000 --project-id <uuid>
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from src.services.project_skills.scanner import scan_skill_document  # noqa: E402
from src.services.project_skills.skill_document import (  # noqa: E402
    SkillDocumentError,
    parse_skill_document,
)

SKILLS_DIR = pathlib.Path(__file__).parent / "kdense"


def scan_all() -> dict[str, str]:
    """Return {skill_name: document_text} for documents passing the scanner."""
    clean: dict[str, str] = {}
    names: set[str] = set()
    failed = False
    for path in sorted(SKILLS_DIR.glob("*.md")):
        text = path.read_text()
        try:
            doc = parse_skill_document(text)
        except SkillDocumentError as error:
            print(f"FAIL  {path.name}: parse error — {error}")
            failed = True
            continue
        result = scan_skill_document(text, existing_names=names)
        blockers = [f for f in result.findings if f.severity == "blocker"]
        warnings = [f for f in result.findings if f.severity != "blocker"]
        status = "FAIL " if blockers else "ok   "
        print(
            f"{status}{path.name}: lines={doc.instruction_line_count} "
            f"tokens={doc.estimated_tokens} blockers={len(blockers)} "
            f"warnings={len(warnings)}"
        )
        for finding in blockers + warnings:
            print(
                f"      {finding.severity} {finding.code} (line {finding.line}): {finding.message}"
            )
        if blockers:
            failed = True
        else:
            clean[doc.name] = text
            names.add(doc.name)
    if failed:
        sys.exit(1)
    return clean


def upload(
    clean: dict[str, str],
    base_url: str,
    project_id: str,
    token: str,
    *,
    self_approve: bool = False,
    acknowledge_warnings: bool = False,
) -> None:
    import httpx

    headers = {"Authorization": f"Bearer {token}"}
    api = f"{base_url.rstrip('/')}/api/v1/projects/{project_id}/skills"

    def approve(client: "httpx.Client", request_id: str) -> None:
        client.post(
            f"{api}/change-requests/{request_id}/approve",
            json={
                "self_approval_acknowledged": True,
                "warning_acknowledged": acknowledge_warnings,
                "audit_note": "Imported from K-Dense scientific-agent-skills (MIT).",
            },
        ).raise_for_status()

    with httpx.Client(headers=headers, timeout=30.0) as client:
        catalog = client.get(api).raise_for_status().json()
        active = {s["name"] for s in catalog["skills"] if s.get("active_version_id")}
        pending = {
            r["skill_name"]: r["id"]
            for r in catalog["pending_change_requests"]
            if r["status"] == "pending"
        }
        for name, text in clean.items():
            if name in active:
                print(f"skip  {name}: already active in catalog")
            elif name in pending:
                if self_approve:
                    approve(client, pending[name])
                    print(f"done  {name}: self-approved existing pending request")
                else:
                    print(f"wait  {name}: pending request awaits reviewer approval")
            else:
                request = (
                    client.post(api, json={"document_text": text})
                    .raise_for_status()
                    .json()
                )
                if self_approve:
                    approve(client, request["id"])
                    print(f"done  {name}: created + self-approved")
                else:
                    print(f"done  {name}: created pending request {request['id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--project-id")
    parser.add_argument(
        "--self-approve",
        action="store_true",
        help="Approve your own change requests (skips reviewer approval).",
    )
    parser.add_argument(
        "--acknowledge-warnings",
        action="store_true",
        help="Acknowledge scanner warnings when self-approving.",
    )
    args = parser.parse_args()

    clean = scan_all()
    print(f"\n{len(clean)} document(s) clean.")
    if args.upload:
        token = os.environ.get("NOUS_TOKEN", "")
        if not (args.project_id and token):
            parser.error("--upload requires --project-id and the NOUS_TOKEN env var")
        upload(
            clean,
            args.base_url,
            args.project_id,
            token,
            self_approve=args.self_approve,
            acknowledge_warnings=args.acknowledge_warnings,
        )


if __name__ == "__main__":
    main()
