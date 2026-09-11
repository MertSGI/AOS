# Dedicated Vercel Host Deployment & ChatGPT MCP Binding Guide

**Implementation Authority ID:** `LARI-AOS-CR2-LITE-DEPLOYABLE-HOST-PACKAGING-20260911-01`  
**Program ID:** `LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01`  
**Production Gate:** `LARI_PRODUCT_PRODUCTION=NO_GO`

---

## 1. Architecture & Security Boundary

The `lari-controller-relay` host packages the verified CR2-Lite Relay ingress surface as an isolated, serverless Streamable HTTP Model Context Protocol (MCP) endpoint.

- **Hosting Provider:** Vercel
- **Canonical Endpoint:** `https://<PROJECT_DOMAIN>/api/mcp`
- **Transport:** Streamable HTTP MCP (JSON-RPC 2.0)
- **Principal Binding:** Permanently bound to `LARI_CONTROLLER`. External callers cannot override principal.
- **Allowed Operations:** Exactly the 7 bounded operations:
  1. `relay_get_head`
  2. `relay_get_latest_unconsumed`
  3. `relay_read_message`
  4. `relay_publish_message`
  5. `relay_publish_receipt`
  6. `authority_fetch_and_verify`
  7. `publish_controller_authority`
- **Zero Generic Operations:** No generic Git push/pull, no arbitrary repository/branch mutation, no shell execution, no product database mutation.

---

## 2. One-Time Vercel Project Import

Deploy by importing the repository via the Vercel Dashboard into the dedicated control plane project:

| Configuration Item | Value | Notes |
| :--- | :--- | :--- |
| **Vercel Team** | `team_zcDmWRX4gZ2SlSwYmZCf8kUP` | Dedicated team namespace |
| **New Project Name** | `lari-controller-relay` | **Do NOT** mutate `lari-staging` (`prj_zmgtS0kmnomhQcGjQ85kVzLD4zzm`) |
| **Import Repository** | `MertSGI/AOS` | Git provider import |
| **Production / Target Branch** | `aos/controller-relay-vercel-host-packaging-r0` | Pinned packaging candidate branch |
| **Root Directory** | `./` (repository root) | Uses `vercel.json` and `api/mcp.py` |
| **Framework Preset** | `Other` | Handled by `@vercel/python` builder |

---

## 3. Required Environment Variables (Encrypted in Vercel UI)

Configure the following environment variable **NAMES** in the Vercel Project Settings (`Settings -> Environment Variables`). Never transport raw secret values through chat or commit them to Git.

| Variable Name | Required | Description |
| :--- | :--- | :--- |
| `LARI_CONTROLLER_SESSION_SECRET` | **YES** | High-entropy shared secret (min 32 characters / 256-bit entropy) for MCP Bearer token authentication. Constant-time verified. |
| `CONTROLLER_RELAY_GITHUB_APP_ID` | **YES** | Numeric App ID of the dedicated Controller Relay GitHub App. |
| `CONTROLLER_RELAY_GITHUB_APP_PRIVATE_KEY_B64` | **YES** | Base64-encoded RSA Private Key (`.pem`) of the dedicated Controller Relay GitHub App. |
| `CONTROLLER_RELAY_GITHUB_APP_INSTALLATION_ID` | **YES** | Numeric installation ID for `MertSGI/AOS` repository access. |
| `CONTROLLER_RELAY_GITHUB_APP_LARI_INSTALLATION_ID` | OPTIONAL | Numeric installation ID for `MertSGI/Randapp-main` authority publication. Defaults to `CONTROLLER_RELAY_GITHUB_APP_INSTALLATION_ID` if identical. |

---

## 4. Preflight Repository Access Verification

Before live tool execution, confirm the GitHub App installation has repository permissions on:
1. `MertSGI/AOS`: Contents (Read & Write) on `control/controller-relay`.
2. `MertSGI/Randapp-main`: Contents (Read & Write) on `control/lari-project-control-plane`.

If the App installation does not cover `Randapp-main`, grant access in GitHub App installation settings before invoking `publish_controller_authority`.

---

## 5. Connecting ChatGPT via Custom MCP Connector

Once deployed to Vercel:

1. **Endpoint URL:**
   ```
   https://lari-controller-relay.vercel.app/api/mcp
   ```
   *(Or the assigned custom/preview domain under `team_zcDmWRX4gZ2SlSwYmZCf8kUP`)*

2. **Authentication Method:**
   - Type: `Bearer Token` / `API Key`
   - Header: `Authorization: Bearer <LARI_CONTROLLER_SESSION_SECRET>`

3. **In ChatGPT Connector / Settings UI:**
   - Add Custom MCP Action / Connector.
   - Set Server URL to: `https://<YOUR_VERCEL_DOMAIN>/api/mcp`
   - Set Authentication to Bearer and supply the secret configured in Vercel.
   - Save connector.

4. **Live Verification Handshake:**
   - Verify that ChatGPT can execute `relay_get_head` and receives current HEAD `7e8037814e3dfda4065d658c05bf44d41f92ab0d`.
