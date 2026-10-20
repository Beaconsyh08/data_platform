# Data Platform CLI

A conversational terminal for your Data Platform account. The platform uses its configured Qwen model;
you do not need a model API key. This client does not install LeRobot, PyTorch or GPU libraries.

Requires Python 3.10+ with `venv`, curl, and access to the platform network (company LAN/VPN).
Run this complete command in a Linux/macOS terminal, without sudo. It downloads the installer,
installs the client and trusted CA, then opens the production username/password login:

```bash
(
  set -e
  installer=$(mktemp)
  trap 'rm -f "$installer"' EXIT
  curl --proto '=https' --tlsv1.2 -fsSL https://algorithm-vla-cli-downloads.oss-cn-beijing.aliyuncs.com/cli/0.2.0/install.py -o "$installer"
  python3 "$installer" --login
)
```

The installer verifies the pinned SHA-256 of the wheel and public CA, then installs into
`~/.local/share/data-platform-cli/0.2.0`. It uses your configured Python package index for dependencies
and does not change your shell profile or system trust store. On Debian/Ubuntu, Python's `venv`
support may require the distribution's `python3-venv` package. Repeat installation reuses the
recognized installation; unrelated existing files are rejected. `--prefix` and `--bin-dir` select
other installation directories. If installation fails, the previous launcher is preserved.

For subsequent logins:

```bash
~/.local/bin/data-platform
```

This launcher defaults to production at `https://10.8.8.40`. To use development:

```bash
~/.local/bin/data-platform --server https://10.8.8.40:8443
```

The download URLs are public and do not expire; platform access still requires your account and
network access. Only the lightweight client, installer and public CA are published.

## Install from source or use another server

The source package requires a platform server with the assistant action APIs:

```bash
pipx install ./clients/data-platform-cli
data-platform --server https://platform.example.com --username your-account
```

For another private CA, obtain the trusted certificate from that platform's administrator:

```bash
data-platform --server https://platform.example.com \
  --ca-bundle /path/to/platform-ca.crt
```

The CLI prompts for your password without echoing it, then lists accessible datasets. Passwords, session
cookies and input history stay in memory; there is no password argument, credential file or TLS bypass.
Exit logs out the session. Conversations are retained by the server under the same policy as the website.
Do not paste credentials into conversation messages.

Type a request such as `帮我准备 Viewer`. The assistant displays the target and parameters before doing
anything. Type `/confirm` to submit the prepared operation, then `/status` to check the run. Viewer
accounts can only ask for explanations. Source mutations, deletion, approval and publication remain in
their dedicated web interfaces.

| Input | Behavior |
|---|---|
| Enter / Alt+Enter | Send / insert a new line |
| Up / Down / Tab | Recall input / complete slash commands |
| `/datasets` | Choose another authorized dataset and start a new conversation |
| `/new` | Start another conversation on the selected dataset |
| `/history` | Show the restored server conversation history |
| `/chats` / `/chats next` | List accessible saved conversations, with bounded pagination |
| `/preferences` | Show personal preferences and editing commands; shared with the web in this environment |
| `/restore <number or conversation-id>` | Restore your own conversation, pending response and action |
| `/resume` | Recover an interrupted response using its original request ID |
| `/confirm` | Confirm the displayed Viewer/Quality operation; retries reuse the same run |
| `/status` | Read the confirmed operation's run and step status |
| `/exit` or Ctrl+D | Log out and exit |

Ctrl+C clears input or interrupts waiting; it does not cancel work already accepted by the server.
If the connection drops, use `/resume`. After restarting the CLI, use the displayed conversation ID with
`/restore`. If a request never reached the server there is nothing to restore. Starting a new conversation
does not cancel the old server request. A new exchange invalidates any older, unconfirmed action card.
Already submitted work can also be followed in the website's Jobs page.

This is an API client for data operations. It cannot read your local files or run shell commands. HTTPS
certificate validation, platform roles, object scopes, input identity checks and CSRF remain enabled.
Response text is rendered as plain text with terminal control sequences removed.

## Personal preferences and history

`/chats` lists your own currently authorized conversations; `/restore 1` opens the first result.
`/history` retains its original meaning: the messages of the current conversation. Context changes
can make an old conversation unavailable; start a new chat instead. History is bounded to six exchanges
per conversation and follows the existing 30-day inactivity/request retention policy.

Preferences are explicit settings shared by web and CLI for the same account and environment:

```text
/preferences language zh
/preferences detail concise
/preferences favorites viewer.prepare,curation.quality
/preferences off
/preferences on
/preferences reset
```

Use `auto` to match message language, `balanced` or `detailed` for other response lengths, and
`/preferences favorites none` to clear favorite operators. At most five business operators can be
selected. Settings affect replies and suggestions on the next message; favorites do not add operations
to a plan. Viewer accounts may manage their own communication preferences but remain read-only for data.
The assistant does not silently infer preferences, train a model, or change operation parameters,
permissions or confirmation requirements. The web Preferences panel can edit or clear the same settings.
Concurrent updates use revisions; reload settings if another client changed them.

## Implementation choice

[prompt-toolkit](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)
provides terminal editing, in-memory history, completion and key bindings. Rich provides waiting feedback;
Requests uses the platform's existing HTTPS cookie session.
[Qwen-Agent](https://github.com/QwenLM/Qwen-Agent) supports model tools and agents, but is not a dependency:
the platform already owns tool authorization, conversation state and confirmation. Keeping that logic on
the server gives web and CLI users the same behavior.

From this directory, `uv build --wheel` produces an installable client wheel. The server and CLI are
released separately; an older server without action APIs cannot confirm operations from this client.
