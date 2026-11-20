# Threat model

Recipe Engine is a per-user process that can move the mouse, press keys and drive
a browser on the machine it runs on. This document says who can make it do what,
what it refuses to do no matter who asks, and what it does not protect against.

## Assets

- **Control of the desktop and the browser** - the only reason the engine exists.
- **Saved recipes** - selectors, UI identities, typed text (never secrets) that
  describe what the user does at work.
- **The browser profile and session snapshot** - cookies that keep the user signed
  in to their portals.
- **The install token** - the bearer token any client needs to call a tool.

## Trust boundaries

| Party | Trusted to | Not trusted to |
|---|---|---|
| The signed-in Windows user | everything: they own the desktop already | - |
| Claude Desktop / Claude Code / Cowork holding the token | call any tool, record and replay recipes | type a secret, press Send/Submit/Buy/Delete, run when the safety self-test fails |
| A recipe file (own or imported) | describe steps | carry cookies, secrets, run history; name a destructive step |
| Other local users, other machines | nothing | reach the port (loopback only), read the token (ACL), read recipes (DPAPI) |

The engine binds **127.0.0.1 only** and requires `Authorization: Bearer <token>`
on every request except `/api/health`. The token is 43 characters of
`secrets.token_urlsafe`, stored in `%LOCALAPPDATA%\RecipeEngine\token` with an ACL
of the current user only, compared in constant time. DNS-rebinding protection is on
(Host/Origin must be loopback). `--bind <adapter>` widens the listener for a
sandboxed client that cannot reach loopback; the token stays mandatory, and the
README says so next to the flag.

## What the engine refuses, whoever asks

These are the 17 protected invariants in `recipe_engine/invariants.py`. They are
checked at every start by reading the source files from disk; while one is broken
every acting tool answers `refused: invariants_failed`. The self-test
(`python -m recipe_engine.invariants --self-test`) applies 43 source mutations and
fails if any of them goes undetected. Changing the list needs the owner's
explicit approval (it is recorded in the commit that changed it).

1. **Never types a password or a code.** Text is checked against one credential
   regex; the focused control is checked for `IsPassword` and for a name like
   Password / CVV / security code / one-time code; browser fills are refused for
   `type=password`, for selectors that name a password, and for credential-looking
   text. A refusal pauses the recording, which is then never saved.
2. **Never presses Send, Submit, Buy, Pay, Post, Delete, Check out ...** (the
   send-whitelist in `config.DESTRUCTIVE_WORDS`). Such steps are dropped while
   recording; a recipe whose name, description, step or UI target names one is
   refused for replay, for scheduling, for export and for import.
3. **Never replays past a login page.** Every browser action runs the login
   detector; a replay stops with `login_required` (not counted as a failure).
4. **Never guesses.** Each step asserts the page/window it expects; the first
   miss stops the replay with the step number and reason; two consecutive
   failures quarantine the recipe until it is re-recorded. A recorded element is
   found by identity (UI Automation); pixel coordinates are used only on an
   identical display, never in background mode.
5. **One owner of the input.** A second replay or live action while one runs is
   answered `busy`, never queued. Esc aborts within 200 ms and releases every key.
6. **No model in the loop.** No model SDK is imported anywhere in the package and
   nothing launches `claude`; replay makes no network calls except to the pages
   the recipe names.
7. **Sealed at rest.** Recipes, the session snapshot, jobs and the export key are
   DPAPI-sealed for the current user; exports are HMAC-signed and carry no
   cookies, storage, secrets or run history; imports re-run every check above.

## What it does not protect against

- **The signed-in user's own account.** Anything the user can do, a client with
  the token can do within the rules above - that is the product. Do not give the
  token to a client you would not let use your keyboard.
- **Malware already running as the user.** It can read the token and the DPAPI
  blobs; the engine adds no new exposure but removes none.
- **Sites that put secrets in ordinary fields.** The credential regex and the
  IsPassword check are heuristics; a site that labels a PIN field "Access number"
  defeats them. The send-whitelist is a word list, not an understanding of intent.
- **Screen content.** Screenshots go to the client that asked for them (a JPEG in
  the tool result) and to the transcript of a failed replay (`screenshot_b64`).
  Both are on the client's side of the boundary.
- **Availability.** A stuck replay is stopped by the 300 s cap and by Esc; the
  engine does not defend against a client calling it in a loop.
- **Physical access / an unlocked session.** The engine only works in an unlocked
  interactive session; it does not unlock one.

## Reporting

Open an issue on the repository. For a problem that lets a client type a secret
or press Send, include the recipe JSON (exported, so it holds no secrets) and the
transcript line from `%LOCALAPPDATA%\RecipeEngine\transcripts\`.
