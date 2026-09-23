# Codex CLI capability evidence

Evidence date: 2026-09-23 (Europe/Istanbul)

Base SHA: `a0cfc2e329b36dca1964ed427ab17d8c82cbacb6`

All behavioral probes ran in newly created temporary Git repositories outside AOS, LARI, protected project workspaces, and live runtime state. Test Codex threads and temporary repositories were deleted after evidence collection. No credentials, token values, API keys, or raw auth files were read or printed.

## Installed CLI and authentication

| Check | Result |
|---|---|
| executable discovered | yes; installed with the OpenAI ChatGPT IDE extension |
| `codex --version` | `codex-cli 0.146.0-alpha.3` |
| `codex login status` | exit `0`; `Logged in using ChatGPT` |
| `OPENAI_API_KEY` present | no |
| `CODEX_ACCESS_TOKEN` present | no |
| `codex doctor --json` auth evidence | `stored auth mode=chatgpt`, stored ChatGPT tokens true, stored API key false |
| subscription auth usable | yes; multiple successful `codex exec` turns and app-server quota read |
| API-key fallback used | no |

Official authentication documentation distinguishes ChatGPT subscription access from API-key usage-based access and states that `codex login status` reports the active method: <https://developers.openai.com/codex/auth/>.

## Non-interactive JSONL execution

A read-only disposable run returned exit `0` and exactly these event types:

```text
thread.started
turn.started
item.completed
turn.completed
```

The `thread.started` object exposed `type,thread_id`. The terminal object exposed `type,usage`; usage fields were `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, and `reasoning_output_tokens`. The final agent message was available as an `item.completed` item with `id,type,text`.

Observed thread UUID: recorded transiently for the test and then deleted. The returned message matched the requested proof token. The disposable Git worktree remained clean.

This matches the documented `codex exec --json` JSONL contract and documented event families: <https://developers.openai.com/codex/noninteractive/>.

### Write, shell, test, and Git proof

A separate `workspace-write` disposable run was instructed to create an exact text file, create one Python `unittest`, run that test, and inspect Git status.

Observed facts:

- exit status `0`;
- terminal `turn.completed` present;
- item types included `file_change` and `command_execution`;
- five command executions completed;
- `proof.txt` matched the exact bytes `CODEX_WRITE_OK` plus one LF;
- `test_preflight.py` existed and the requested test completed before the final message;
- Git status showed only the requested untracked proof/test files and Python's generated test cache;
- final agent text was the requested proof token.

Therefore the tested CLI installation proves repository write, shell execution, test execution, and Git inspection in a bounded disposable repository. It does not prove OS-level containment; AOS must continue to provide workspace, process-tree, authority, and post-execution verification boundaries.

## Exit status behavior

| Condition | Observed result |
|---|---|
| `codex --version` | exit `0` |
| ChatGPT `codex login status` | exit `0` |
| valid `codex exec --json` turn | exit `0`, `turn.completed` |
| valid exact-id resume | exit `0`, same thread UUID, `turn.completed` |
| invalid UUID resume | exit `1`, no JSONL events, structured stderr says no rollout found |
| invalid argument placement | exit `2` from CLI argument parser |
| forced interruption immediately after `thread.started` | only `thread.started`; no terminal event; process was externally terminated |
| resume of that immediately interrupted thread | exit `1`; persisted rollout was empty and unreadable |

Adapter success must require both exit `0` and a valid terminal `turn.completed`. Exit status alone is not sufficient, and a thread UUID alone is not a durable checkpoint.

## Session identity and resume

The initial JSONL event exposed a UUID `thread_id`. `codex exec resume <UUID>` in the original working directory returned the same UUID and completed successfully.

The same exact UUID was then resumed from a different disposable Git repository. The CLI accepted the resume, preserved the UUID, and the agent reported the new working directory. The CLI therefore does not protect AOS from resuming a context against changed workspace contents.

An additional `--last` probe from a fresh directory exited `0` but returned a different thread UUID. Regardless of whether it selected or synthesized that thread, it did not provide the exact identity semantics AOS requires. Durable automation must never use `--last`.

The CLI reference documents exact-id resume and describes `--last` as working-directory filtered: <https://developers.openai.com/codex/cli/reference/>. Local behavior is the binding evidence for version `0.146.0-alpha.3`.

## Working-directory behavior

`-C <dir>` targets a working root for new execution. Exact-id resume can also run when the process working directory changes. No automatic source SHA or content fingerprint comparison was observed. AOS must perform its own pre-resume workspace fingerprint check.

## Quota and status observability

Three supported surfaces were evaluated:

1. `codex login status` is useful for login presence/mode but is human text.
2. `codex doctor --json` is a redacted machine-readable health/auth/runtime report. It exposed structured authentication mode, binary version, provider reachability, WebSocket reachability, and state integrity; it did not expose quota.
3. The stable `codex app-server` JSONL protocol supports `account/rateLimits/read` and `account/rateLimits/updated`.

A live read-only app-server handshake returned a structured `account/rateLimits/read` result with:

- `rateLimits`, `rateLimitsByLimitId`, and `rateLimitResetCredits`;
- bucket id `codex`;
- plan type `plus`;
- primary used percentage `38`;
- secondary used percentage `6`;
- reset timestamps for both windows;
- `rateLimitReachedType=null`;
- credit flags present.

The current observation classifies as `AVAILABLE`; this is a point-in-time observation, not a guarantee. The official app-server documentation defines the rate-limit method and fields: <https://developers.openai.com/codex/app-server/>.

The generated stable app-server schema (without experimental opt-in) also contained `GetAccountRateLimitsResponse` and `AccountRateLimitsUpdatedNotification`. Quota is therefore available to AOS as structured data without scraping `/status` prose.

## Data available without prose scraping

| Surface | Machine-readable data |
|---|---|
| `codex exec --json` | thread UUID; turn start/completion/failure; item type/status; agent final message; command/file/tool events; token usage; structured errors |
| `codex doctor --json` | version; auth storage/mode booleans; provider/network reachability; runtime/config/state checks; redacted support evidence |
| app-server `account/rateLimits/read` | usage percentages, reset times/windows, plan type, credits, spend-control fields, reached-limit classification, multiple limit buckets |
| app-server notifications | sparse rolling `account/rateLimits/updated` observations |
| process result | exit code, timeout/interruption, stderr presence after AOS redaction |

Raw reasoning, raw tool parameters, full transcripts, auth files, and human-formatted status output are neither needed nor acceptable as the AOS orchestration contract.

## Capability verdicts

| Capability | Verdict | Evidence |
|---|---|---|
| machine-readable noninteractive execution | proven | successful JSONL turns |
| `codex exec --json` | proven | parsed event and usage fields |
| exit status behavior | proven for success, parser failure, missing session, and forced interruption | bounded probes |
| session/thread identity | proven | UUID in `thread.started` |
| exact prior-session resume | proven | same UUID resumed successfully |
| resume after working-directory change | proven accepted by CLI; unsafe without AOS gate | cross-repository resume probe |
| repository read/write | proven | read-only and exact write probes |
| shell/test/Git execution | proven | structured command events and completed disposable unit test/Git inspection |
| structured auth diagnostics | proven | `doctor --json` |
| structured quota status | proven | stable app-server request/response |
| actual exhausted-quota response | not induced | must be covered by failure injection and later natural observation |
| safe resume after mid-turn hard kill | not guaranteed | immediate interruption produced an empty, non-resumable rollout |

## Integration-specific parser observations

- `--ask-for-approval` is a top-level option on this binary and must precede `exec`.
- When the parent host presents stdin as piped while also supplying a prompt argument, Codex prints `Reading additional input from stdin...` on stderr. The backend should pass the prompt through stdin and use `-` explicitly.
- PowerShell shell-snapshot warnings may appear on stderr even when a run succeeds. Do not classify every non-empty stderr as failure; use exit status plus structured terminal events and a bounded warning taxonomy.
- The tested CLI reports an alpha version. Executable/version/schema attestation and fail-closed contract tests are mandatory before every production-candidate activation.

## Evidence limits

- No real quota exhaustion was forced.
- No capability probe touched a production workspace, LARI workspace, protected workspace, remote, API key, paid endpoint, or live AOS runtime. The separately authorized docs-only branch push is not part of the probe.
- The write/test proof establishes behavior in a disposable repository, not a general containment guarantee.
- Official docs and generated schemas describe a moving product surface; implementation must pin and probe the exact post-Track-A installation.
