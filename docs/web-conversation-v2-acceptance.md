# Real-client web conversation v2 acceptance

Run only after the matching Relay deployment is live. This uses real inference,
real LocalSession/CloudAgentRuntime, real read/vision tools, and private OSS uploads.
It spends model credits, creates a dedicated project and chat, and keeps those
artifacts for inspection. It never changes account preferences, purchases anything,
or deletes existing data. No credential refresh or persistent login is performed;
optional test-account login is explicit and process-only.

The client JWT must belong to an existing **CLI** login; a browser JWT cannot
register a client chat. The web JWT must belong to the same account. Supply both
through a protected environment or hidden shell input, never command arguments,
files, shell history, tracing, or a committed `.env`.

Example isolated **zsh** subshell (replace the model ID with an available official
manual main-agent model; no HOME or existing client-state changes):

```zsh
(
  set +x
  umask 077
  read -rs 'AERO_E2E_CLIENT_JWT?CLI JWT (hidden): '
  print
  read -rs 'AERO_E2E_WEB_JWT?Web JWT (hidden): '
  print
  export AERO_E2E_CLIENT_JWT AERO_E2E_WEB_JWT
  export AERO_OFFICIAL_API_URL=https://api.aerolytica.skyviewor.team
  export AERO_OFFICIAL_LLM_URL=https://llm.aerolytica.skyviewor.team/v1
  acceptance_dir=$(mktemp -d /tmp/aero-acceptance.XXXXXXXX)
  /Users/clarmylee/github/Aerolytica/.venv/bin/python \
    /Users/clarmylee/github/Aerolytica/scripts/accept_web_conversation_v2.py \
    --workspace "$acceptance_dir" --manual-model YOUR_MAIN_MODEL_ID \
    --allow-model-charges
)
```

### Optional environment-only login acquisition

Instead of supplying both JWTs, pass `--login-from-env` and supply
`AERO_E2E_TEST_ACCOUNT` (email or phone) or `AERO_E2E_TEST_PHONE`, plus
`AERO_E2E_TEST_PASSWORD`, through protected runtime environment/hidden shell input.
The script fills only missing JWTs; an already supplied web JWT is reused. Never
write a `.env` file or pass credentials as CLI arguments. No tokens are printed.

Relay's login wire is `POST /v1/auth/login` with JSON
`{account, password, remember_me:false}`. Session kind is **not** a JSON field:

- CLI: `X-Aero-Client: cli`, with optional `X-Aero-Device` / `X-Aero-OS`.
- Web: `X-Aero-Client: web` and `X-Relay-Web-Auth: 1`.

Browser-style login honors CAPTCHA. If required, supply valid user-acquired
`AERO_E2E_CAPTCHA_TICKET` / `AERO_E2E_CAPTCHA_RANDSTR`, or provide an existing
authenticated browser JWT instead. The script does not bypass CAPTCHA or acquire
it automatically. It uses separate in-memory HTTPX clients for the two logins,
does not reuse their cookies, discards refresh tokens, and keeps access JWTs only
in the protected process environment. It creates new test login sessions without
revoking or cleaning existing account sessions.

For hidden zsh input, replace the two JWT `read` lines with:

```zsh
read -rs 'AERO_E2E_TEST_PHONE?Test phone (hidden): '
print
read -rs 'AERO_E2E_TEST_PASSWORD?Test password (hidden): '
print
export AERO_E2E_TEST_PHONE AERO_E2E_TEST_PASSWORD
# Add --login-from-env to the acceptance invocation.
```

The subprocess uses an empty dedicated workspace, separate encrypted session
storage with an in-memory key, a dedicated vision cache, and no persisted account
credentials. Only real `read_file` and `analyze_image` tools are enabled. All remote
execution and inference are real; only credential loading and local state paths
are isolated. Small test attachments and cloud artifacts remain; no cleanup is
automated. Local encrypted session files cannot be reopened after the process
exits because their encryption key is intentionally not saved.

### Existing-browser handoff without extracting its JWT

Use `--http-use-cli-bearer --login-from-env --linger-seconds 120` with protected
test-account credentials and the same isolated invocation above. Only a CLI login
is acquired; scripted remote HTTP assertions use that bearer. This explicitly does
**not** test browser login. No browser JWT extraction or CAPTCHA bypass occurs.
Without CLI-bearer mode, a required web CAPTCHA must be completed by the user;
the runner will fail safely rather than silently falling back.

After scripted checks succeed, a non-secret `browser_handoff` line prints the
dedicated agent/session IDs and report path. In the already authenticated browser,
open that dedicated chat and submit a harmless QA command, for example “Recall the
continuity marker from this conversation.” The same real restricted runtime stays
alive for the requested interval (0–3600 seconds, default 0), with only read/vision
tools and the isolated workspace. Browser commands can incur model charges.
The report records additional turn IDs and request wire fields, never command text
or credentials. These observations alone do not claim browser UI or fee acceptance;
verify the UI delivery/completion and usage separately. At expiry the runtime stops
and releases its lease; no account artifacts are deleted.

The non-secret `acceptance.json` records IDs, per-question usage, and observed
model/mode/turn/purpose wire fields. Success requires:

- A genuine local foreground turn, then plan/execute/qa web commands in that same
  LocalSession, with continuity markers carried across turns.
- Text attachment bytes read through tools, never expanded into the incoming
  model prompt; an image processed by the real vision tool.
- Correct temporary model/mode choices and restoration afterward.
- At least one `main` call per question using its selected model/mode. Tool
  `vision` calls use the independently resolved account vision model (which can
  differ from the main model), share the turn/fee identity, and do not consume
  main rounds. `title`/`compact` calls are excluded from main routing assertions.
- `relay_turn_id` and `x-relay-turn-id` equal to `command_id`, including stable
  request identity on retries; only headers carry question-purpose metadata.
- Usage for each web message associated with its command's turn ID, and the
  stable current cloud context containing the local continuity marker.

The runner explicitly imports the real read/vision implementations before building
its restricted registry; a fresh-process regression prevents empty-tool startup.
Failures record allowlisted `failure_code` values (for example
`context_file_marker_missing`, `wire_main_model_mode_mismatch`, or
`question_fee_association_mismatch`), plus sanitized observed wire/tool names.
Raw exception/HTTP response text, credentials, and attachment contents are omitted.

Expect prerequisite failures for expired JWTs, mismatched accounts, missing model
availability, offline deployment, concurrency/storage entitlement, or an account
round cap below four. The script does not alter those conditions or silently
retry an ambiguous command. Rerunning creates a new dedicated conversation.

## Client wire contract

Account preferences are JWT-authenticated `GET /v1/agents/preferences`. Relay owns
custom-prompt injection. Before automatic compaction or main inference, local
questions atomically freeze preferences with JWT-authenticated
`POST /v1/agents/preferences/questions` and `{turn_id}`. Its `{turn_id,max_rounds,version}`
snapshot is reused for the whole question; GET is for display, not admission.
Remote commands use their already enqueue-frozen fields without another POST.
Remote commands carry `command_id`, `message_id`, `model`
(`auto` or a manual ID), `mode`, `attachments`, and frozen `max_rounds` /
`preference_version`. The client also accepts `default` as an internal model alias
for `auto`; Relay's web enqueue protocol uses `auto`, not `default`.

An attachment carries `attachment_id`, `filename`, `content_type`, `size_bytes`, and
immutable revision metadata. The client requests
`POST /v1/agents/{agent_id}/messages/{message_id}/attachments/{attachment_id}/download-url`
and uses its `{url, filename, expires_in}` response with a separate, credential-free
HTTPS downloader. V2 capability is advertised through the agent-token capability
endpoint and heartbeat only by entry points implementing this protocol.

After latest context is deleted, a 404 with a newer reported context version drops
old durable compaction events; synchronization uploads current stable context
only, never replaying those deleted archives.

## Verification (2026-10-02)

- Focused client/policy/protocol regression run: **162 passed**, including
  environment-only login/security, separate vision-purpose, and wire-routing tests.
- Latest login/wire/policy run: **32 passed**, including CLI-only login and linger
  lifecycle regressions.
- Final full suite: **857 passed, 30 failed**, two environment/deprecation warnings.
- Full Ruff: **469 issues**; zero issues on modified lines. New client modules,
  regression tests, and the acceptance script pass their focused Ruff checks.
- Script `--help` verified; live paid E2E has **not** been run. It requires the
  deployed protocol and protected CLI/web JWT environment variables.

The baseline replay using HEAD implementations reproduced the following failure
groups; these are not repaired by this client change:

| Test module | Failing behaviors |
| --- | --- |
| `test_chat_images.py` | resident foreground reply fixture lacks session manager |
| `test_chat_presence.py` | sync-once expectation; recovered-append fixture lacks idle callback |
| `test_cli_llm_setup.py` | primary vision reuse; bare-key masking; legacy-model migration; reused vision status; model-switch profile sync |
| `test_cli_math_rendering.py` | help expects hidden BYOK commands |
| `test_era5_adapter.py` | NetCDF time/area and vertical-level subsets |
| `test_experiments.py` | continued-session experiment list |
| `test_ghcn_daily.py` | station/date overlap; preferred NCEI station CSV |
| `test_ifs_adapter.py` | HTTP 429 retry behavior |
| `test_ncep_reanalysis.py` | NCSS fallback/subset |
| `test_official_model_picker.py` | model picker metadata expectations |
| `test_remote_agent.py` | legacy removed upload-memory API expectation |
| `test_setup_wizard.py` | official source; preset key help; missing-key setup; reused form; connection failure; model picker; picker scrolling |
| `test_tool_execution_policy.py` | four official-vision fixtures mock legacy flow instead of OSS upload handshake |
| `test_web_server.py` | primary setup API-key propagation (unchanged implementation path) |

Reproducible implementation baseline is commit
`59573032d5c760837f5e52e554c419306384db77`. The replay loads eight changed client
modules from that commit in memory; it does not reset, stash, overwrite, or check
out files. User-dirty model catalog edits and the dependency/environment state
remain unchanged. Current tests are used, so exclude newly added protocol tests
and intentional new-behavior expectations when comparing the old implementation.

The original broad baseline replay returned **801 passed / 31 failed**, excluding
the web-server module and newly added tests. A subsequent grouped replay of all
final failing modules plus the specific web setup selector returned **209 passed /
31 failed**: **every one of the final 30 failures reproduced on the old client
implementations**, plus the intermittent setup-wizard success-timing failure.
The web setup failure can also be replayed explicitly:

```sh
.venv/bin/python scripts/replay_client_baseline.py \
  tests/test_web_server.py::test_primary_setup_tests_before_persisting
```

For grouped comparison of every final failure, use the same helper with the test
modules listed in the table (and the specific web setup selector above). No new
feature is assumed to pass merely because a failure is old: focused regression
coverage separately verifies account admission, frozen limits, purpose headers,
stable retry identity, staging, command overrides, and reset/archive behavior.

The baseline also has an intermittent setup-wizard success-timing failure. The
compaction fixture was updated to reflect the real session transcript and to
verify atomic preference freezing before compaction; it now passes. Existing
dirty official-model edits and unrelated untracked artifacts were preserved.
