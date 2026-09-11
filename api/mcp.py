"""Vercel Serverless Function entrypoint for Streamable HTTP MCP.

Canonical Endpoint: /api/mcp
Implementation Authority: LARI-AOS-CR2-LITE-DEPLOYABLE-HOST-PACKAGING-20260911-01
Program ID: LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01

Environment Variables Required:
- LARI_CONTROLLER_SESSION_SECRET (min 32 chars, constant-time verification)
- CONTROLLER_RELAY_GITHUB_APP_ID
- CONTROLLER_RELAY_GITHUB_APP_PRIVATE_KEY_B64
- CONTROLLER_RELAY_GITHUB_APP_INSTALLATION_ID (or CONTROLLER_RELAY_GITHUB_APP_AOS_INSTALLATION_ID)
- CONTROLLER_RELAY_GITHUB_APP_LARI_INSTALLATION_ID (optional, defaults to above)

Supports WSGI/ASGI or Vercel HTTP handler interface.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Optional

# Ensure 'src' is in path if deployed from repository root
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from aos.controller_relay_authority_resolver import (
    AuthorityArtifactResolver,
    CANONICAL_LARI_AUTHORITY_REPOSITORY,
)
from aos.controller_relay_github_app import (
    GitHubAppInstallationTokenManager,
    parse_private_key_input,
)
from aos.controller_relay_git_transport import (
    FIXED_RELAY_REPOSITORY,
    GitDataCASRelayTransport,
    StdlibGitHubRequester,
)
from aos.controller_relay_mcp_handler import McpHttpHandler
from aos.controller_relay_service import ControllerRelayService
from aos.lari_controller_ingress import (
    LariControllerIngressError,
    LariControllerIngressService,
    create_secret_session_authenticator,
)


def create_hosted_mcp_handler() -> McpHttpHandler:
    """Initialize the MCP HTTP handler from environment variables.

    FAILS CLOSED if session secret is missing or insufficient.
    """
    session_secret = os.environ.get("LARI_CONTROLLER_SESSION_SECRET")
    if not session_secret or len(session_secret.strip()) < 32:
        raise RuntimeError(
            "STARTUP_FAIL_CLOSED: LARI_CONTROLLER_SESSION_SECRET environment variable "
            "must be configured with at least 32 characters."
        )

    session_authenticator = create_secret_session_authenticator(session_secret.strip())

    # GitHub App configuration
    app_id = os.environ.get("CONTROLLER_RELAY_GITHUB_APP_ID")
    private_key_raw = os.environ.get("CONTROLLER_RELAY_GITHUB_APP_PRIVATE_KEY_B64") or os.environ.get(
        "CONTROLLER_RELAY_GITHUB_APP_PRIVATE_KEY"
    )
    inst_id_aos = os.environ.get("CONTROLLER_RELAY_GITHUB_APP_AOS_INSTALLATION_ID") or os.environ.get(
        "CONTROLLER_RELAY_GITHUB_APP_INSTALLATION_ID"
    )
    inst_id_lari = os.environ.get("CONTROLLER_RELAY_GITHUB_APP_LARI_INSTALLATION_ID") or inst_id_aos

    if not app_id or not private_key_raw or not inst_id_aos:
        raise RuntimeError(
            "STARTUP_FAIL_CLOSED: Missing required GitHub App credentials in environment. "
            "Must provide CONTROLLER_RELAY_GITHUB_APP_ID, CONTROLLER_RELAY_GITHUB_APP_PRIVATE_KEY_B64, "
            "and CONTROLLER_RELAY_GITHUB_APP_INSTALLATION_ID."
        )

    private_key_pem = parse_private_key_input(private_key_raw)

    # Token manager for MertSGI/AOS (Relay mutations)
    aos_token_manager = GitHubAppInstallationTokenManager(
        app_id=app_id,
        private_key_pem=private_key_pem,
        installation_id=inst_id_aos,
    )

    # Token manager for MertSGI/Randapp-main (Authority mutations / reads)
    if inst_id_lari and inst_id_lari != inst_id_aos:
        lari_token_manager = GitHubAppInstallationTokenManager(
            app_id=app_id,
            private_key_pem=private_key_pem,
            installation_id=inst_id_lari,
        )
    else:
        lari_token_manager = aos_token_manager

    # Relay transport & service
    relay_cred_provider = aos_token_manager.get_credential_provider()
    relay_requester = StdlibGitHubRequester(credential_provider=relay_cred_provider)
    relay_transport = GitDataCASRelayTransport(requester=relay_requester)
    relay_service = ControllerRelayService(transport=relay_transport)

    # Authority requester & resolver
    auth_cred_provider = lari_token_manager.get_credential_provider()
    auth_requester = StdlibGitHubRequester(credential_provider=auth_cred_provider)

    ingress_service = LariControllerIngressService(
        relay_service=relay_service,
        session_authenticator=session_authenticator,
        authority_requester=auth_requester,
    )

    return McpHttpHandler(ingress_service=ingress_service)


# Lazy handler singleton
_CACHED_HANDLER: Optional[McpHttpHandler] = None
_STARTUP_ERROR: Optional[str] = None


def get_or_create_handler() -> McpHttpHandler:
    global _CACHED_HANDLER, _STARTUP_ERROR
    if _STARTUP_ERROR is not None:
        raise RuntimeError(_STARTUP_ERROR)
    if _CACHED_HANDLER is None:
        try:
            _CACHED_HANDLER = create_hosted_mcp_handler()
        except Exception as exc:
            _STARTUP_ERROR = str(exc)
            raise
    return _CACHED_HANDLER


class handler(BaseHTTPRequestHandler):
    """Vercel Python Serverless Function HTTP Handler."""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""
        auth_header = self.headers.get("Authorization")

        try:
            mcp_handler = get_or_create_handler()
            status_code, resp_dict = mcp_handler.handle_request(raw_body, auth_header)
        except Exception as exc:
            status_code = 500
            resp_dict = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": f"Server startup/initialization error: {exc}"},
            }

        resp_bytes = json.dumps(resp_dict).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

    def do_GET(self):
        """Readiness / info check without exposing secrets."""
        info = {
            "service": "lari-controller-relay",
            "endpoint": "/api/mcp",
            "protocol": "STREAMABLE_HTTP_MCP",
            "status": "HEALTHY",
        }
        resp_bytes = json.dumps(info).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)
