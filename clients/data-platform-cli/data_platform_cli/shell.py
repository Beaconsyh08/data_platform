"""Conversational terminal; passwords and chat history are never written to disk."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import unicodedata

from data_platform_cli.client import ClientError, PlatformClient

COMMANDS = ["/help", "/datasets", "/new", "/history", "/restore", "/resume", "/confirm", "/status", "/exit"]


def safe_terminal(value):
    # Strip OSC/CSI and control bytes; model/server text never becomes terminal commands or markup.
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf"})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chat with your Data Platform using your platform account.")
    parser.add_argument("--server", required=True, help="HTTPS platform origin")
    parser.add_argument("--username")
    parser.add_argument("--ca-bundle", help="CA certificate file for an internal deployment")
    args = parser.parse_args(argv)
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.key_binding import KeyBindings
    from rich.console import Console

    console = Console()

    def say(text):
        console.print(safe_terminal(text), markup=False, highlight=False)

    try:
        client = PlatformClient(args.server, ca_bundle=args.ca_bundle)
    except ClientError as error:
        parser.error(str(error))
    keys = KeyBindings()

    @keys.add("escape", "enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    prompt = PromptSession(completer=WordCompleter(COMMANDS), key_bindings=keys)

    def display(result):
        say(result.get("message") or result.get("error") or "Response received.")
        action = client.last_action
        if action:
            say(f"{action['title']} · Dataset: {action['dataset']} · Node: {action['node']}")
            say(json.dumps(action["parameters"], ensure_ascii=False, indent=2) or "Default parameters")
            if action.get("execution"):
                say(f"Task: {action['execution']['status']} · /status for progress")
            elif action.get("is_current") is False:
                say("Action is stale. Ask to prepare it again.")
            elif action["can_execute"]:
                say("/confirm to submit this operation. Nothing has run yet.")
            else:
                say("Blocked: " + "; ".join(item["reason"] for item in action["blocked"]))
        if result.get("proposal"):
            say("Pipeline draft ready. Open it in the web editor:")
            say(
                client.origin
                + "/pipelines?location_id="
                + client.location["location_id"]
                + "&assistant_conversation="
                + client.conversation["conversation_id"]
            )
        say("Conversation: " + client.conversation["conversation_id"])

    def choose_dataset():
        rows = client.datasets()
        for number, item in enumerate(rows, 1):
            say(f"{number}. {item.get('dataset_key', item['location_id'])} · {item.get('node_name', '')}")
        if not rows:
            raise ClientError("No accessible datasets. Register a dataset in the web console first.")
        selected = prompt.prompt("Dataset number: ")
        if not selected.isdigit() or not 1 <= int(selected) <= len(rows):
            raise ClientError("Choose a dataset number from the list.")
        client.select(rows[int(selected) - 1])
        say("Dataset selected. Describe your request; /help lists commands.")

    try:
        username = args.username or prompt.prompt("Username: ")
        user = client.login(username, getpass.getpass("Password: "))
        say(f"Signed in as {user['username']} ({user['role']}). Password and session stay in memory.")
        choose_dataset()
        while True:
            try:
                message = prompt.prompt("You › ").strip()
                if not message:
                    continue
                if message == "/exit":
                    break
                if message == "/help":
                    say(
                        " · ".join(COMMANDS)
                        + "\nEnter sends. Alt+Enter inserts a new line. Ctrl+C clears input; Ctrl+D exits."
                    )
                elif message == "/datasets":
                    choose_dataset()
                elif message == "/new":
                    client.new_chat()
                    say("New conversation.")
                elif message == "/history":
                    for item in client.conversation["history"]:
                        say(item["role"] + ": " + item["content"])
                elif message.startswith("/restore "):
                    value = client.restore(message.split(maxsplit=1)[1])
                    say("Conversation restored.")
                    for item in value["history"]:
                        say(item["role"] + ": " + item["content"])
                    display({"message": "Use /resume if a response is pending."})
                elif message == "/resume":
                    with console.status("Waiting for assistant…"):
                        result = client.resume()
                    display(result)
                elif message == "/confirm":
                    receipt = client.confirm()
                    say(f"Task submitted: {receipt['run_id']} · {receipt['status']}\n/status for progress")
                elif message == "/status":
                    say(json.dumps(client.status(), ensure_ascii=False, indent=2))
                elif message.startswith("/"):
                    say("Unknown command. Use /help.")
                else:
                    with console.status("Assistant is working…"):
                        result = client.chat(message)
                    display(result)
            except KeyboardInterrupt:
                say("Input interrupted. Already submitted server work continues; use /resume or /status.")
            except (ClientError, ValueError) as error:
                say(str(error))
    except (EOFError, KeyboardInterrupt):
        pass
    except ClientError as error:
        say(str(error))
        return 1
    finally:
        if client.user:
            try:
                client.logout()
            except ClientError:
                say("Could not revoke session immediately; it will expire on the server.")
    return 0
