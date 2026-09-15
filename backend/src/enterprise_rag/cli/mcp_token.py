"""Issue, list, or revoke MCP tokens from a trusted local terminal."""

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from enterprise_rag.adapters.database import Database
from enterprise_rag.config import load_settings
from enterprise_rag.mcp.access import ALL_MCP_SCOPES
from enterprise_rag.mcp.local_credentials import resolve_mcp_token_pepper
from enterprise_rag.mcp.tokens import McpTokenOperator

DEFAULT_ACTOR = "demo-operator@example.invalid"


def main() -> None:
    parser = _parser()
    arguments = parser.parse_args()
    asyncio.run(_run(arguments))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="demo", help="Tenant slug (default: demo)")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="Issue one token and print it once")
    issue.add_argument("--actor-email", default=DEFAULT_ACTOR)
    issue.add_argument("--name", default="Mac local MCP client")
    issue.add_argument("--expires-days", type=int, default=30)
    issue.add_argument("--collection", action="append", default=[])
    issue.add_argument("--scope", action="append", choices=sorted(ALL_MCP_SCOPES), default=[])
    commands.add_parser("list", help="List non-secret token metadata")
    revoke = commands.add_parser("revoke", help="Revoke a token by ID")
    revoke.add_argument("token_id", type=UUID)
    return parser


async def _run(arguments: argparse.Namespace) -> None:
    settings = load_settings()
    database_secret = settings.credentials.database_url
    if database_secret is None:
        raise SystemExit("DATABASE_URL is required")
    configured = settings.credentials.mcp_token_pepper
    pepper = resolve_mcp_token_pepper(
        configured.get_secret_value() if configured is not None else None,
        path=_pepper_path(settings.ingestion.object_store_root),
        production=settings.app.environment == "production",
    )
    database = Database(database_secret.get_secret_value())
    operator = McpTokenOperator(database, pepper=pepper)
    try:
        if arguments.command == "issue":
            collections = tuple(UUID(value) for value in arguments.collection)
            scopes = tuple(arguments.scope) or tuple(sorted(ALL_MCP_SCOPES))
            issued = await operator.issue(
                tenant_slug=arguments.tenant,
                actor_email=arguments.actor_email,
                name=arguments.name,
                collection_ids=collections or None,
                scopes=scopes,
                expires_in=timedelta(days=arguments.expires_days),
            )
            metadata = {
                "id": str(issued.id),
                "name": issued.name,
                "token_prefix": issued.token_prefix,
                "scopes": list(issued.scopes),
                "collection_ids": [str(value) for value in issued.collection_ids],
                "expires_at": issued.expires_at.isoformat(),
                "notice": "raw token is printed once to stdout",
            }
            sys.stderr.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
            print(issued.raw_token)
            return
        if arguments.command == "list":
            items = await operator.list(tenant_slug=arguments.tenant)
            print(
                json.dumps(
                    {"items": [item.to_dict() for item in items]},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return
        revoked = await operator.revoke(
            tenant_slug=arguments.tenant, token_id=arguments.token_id
        )
        print(json.dumps({"id": str(arguments.token_id), "revoked": revoked}, sort_keys=True))
    finally:
        await database.dispose()


def _pepper_path(object_store_root: Path) -> Path:
    return object_store_root.resolve().parent / "mcp-token-pepper"


if __name__ == "__main__":
    main()
