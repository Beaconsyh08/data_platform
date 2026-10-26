"""Create and revoke platform MCP credentials without printing secrets to the terminal."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path

from data_platform_cli.client import ClientError


def manage(client, args):
    if args.command == "mcp-token" and not args.token_file:
        raise ClientError("mcp-token requires --token-file; the credential will not be printed")
    target = Path(args.token_file).expanduser() if args.token_file else None
    if args.command == "mcp-token" and (target.exists() or target.is_symlink()):
        raise ClientError("Choose a new token file; existing files are never overwritten")
    client.login(args.username or input("Platform username: "), getpass.getpass("Platform password: "))
    try:
        if args.command == "mcp-tokens":
            print(json.dumps(client.request("GET", "/api/mcp/credentials"), indent=2))
        elif args.command == "mcp-revoke":
            import uuid

            try:
                identity = str(uuid.UUID(args.credential_id or ""))
            except ValueError:
                raise ClientError("mcp-revoke requires --credential-id UUID") from None
            client.request("DELETE", "/api/mcp/credentials/" + identity)
            print("MCP credential revoked")
        else:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            credential = None
            try:
                with os.fdopen(descriptor, "w") as stream:
                    credential = client.request(
                        "POST",
                        "/api/mcp/credentials",
                        {
                            "name": args.token_name,
                            "days": args.token_days,
                            "scopes": ["read"] if args.read_only else ["read", "prepare"],
                        },
                    )
                    stream.write(credential["token"] + "\n")
                print("MCP credential saved to " + str(target.absolute()))
                print("Credential ID: " + credential["credential_id"])
                print("MCP URL: " + client.origin + "/mcp")
                print(
                    "Set DATA_PLATFORM_TOKEN from that file in your client environment. Keep your model API key separate."
                )
            except Exception:
                if credential:
                    client.request("DELETE", "/api/mcp/credentials/" + credential["credential_id"])
                target.unlink(missing_ok=True)
                raise
    finally:
        client.request("POST", "/api/auth/logout", {})
    return 0
