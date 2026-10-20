# Data Platform CLI

A conversational terminal for your Data Platform account. The platform uses its configured Qwen model;
you do not need a model API key. This client does not install LeRobot, PyTorch or GPU libraries.

Requires Python 3.10+ and a platform server with the assistant action APIs.

```bash
pipx install ./clients/data-platform-cli
data-platform --server https://platform.example.com --username your-account
```

For a private CA, obtain the trusted certificate from your administrator:

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
| `/restore <conversation-id>` | Restore your own conversation, pending response and action |
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

## Implementation choice

[prompt-toolkit](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)
provides terminal editing, in-memory history, completion and key bindings. Rich provides waiting feedback;
Requests uses the platform's existing HTTPS cookie session.
[Qwen-Agent](https://github.com/QwenLM/Qwen-Agent) supports model tools and agents, but is not a dependency:
the platform already owns tool authorization, conversation state and confirmation. Keeping that logic on
the server gives web and CLI users the same behavior.

From this directory, `uv build --wheel` produces an installable client wheel. The server and CLI are
released separately; an older server without action APIs cannot confirm operations from this client.
