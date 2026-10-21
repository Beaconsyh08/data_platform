"""Conversational terminal; passwords and chat history are never written to disk."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import unicodedata

from data_platform_cli import __version__
from data_platform_cli.client import ClientError, PlatformClient

COMMANDS = [
    "/help",
    "/datasets",
    "/new",
    "/history",
    "/chats",
    "/restore",
    "/preferences",
    "/resume",
    "/confirm",
    "/status",
    "/open",
    "/web",
    "/confirm again",
    "/exit",
]


def safe_terminal(value):
    # Strip OSC/CSI and control bytes; model/server text never becomes terminal commands or markup.
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf"})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chat with your Data Platform using your platform account.")
    parser.add_argument("--version", action="version", version=f"Data Platform CLI {__version__}")
    parser.add_argument("command", nargs="?", choices=["update"], help="Update the installed CLI")
    parser.add_argument("--server", help="HTTPS platform origin")
    parser.add_argument("--username")
    parser.add_argument("--ca-bundle", help="CA certificate file for an internal deployment")
    parser.add_argument("--web", action="store_true", help="Print the web console URL without logging in")
    args = parser.parse_args(argv)
    if args.command == "update":
        from data_platform_cli.update import update

        return update()
    if not args.server:
        parser.error("--server is required for login or --web")
    try:
        client = PlatformClient(args.server, ca_bundle=args.ca_bundle)
    except ClientError as error:
        parser.error(str(error))
    if args.web:
        print(safe_terminal(client.web_links()[0]["url"]))
        return 0
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.key_binding import KeyBindings
    from rich.console import Console

    console = Console()

    def say(text):
        console.print(safe_terminal(text), markup=False, highlight=False)

    say(f"Data Platform CLI {__version__} · Web console: {client.origin}/")
    say("/web shows browser links. Sign in separately in your browser with the same account.")
    say("Exit: /exit, exit, quit, or Ctrl+D on an empty input line.")
    keys = KeyBindings()

    @keys.add("escape", "enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    prompt = PromptSession(completer=WordCompleter(COMMANDS), key_bindings=keys)
    chats, chat_cursor = [], None

    def display(result):
        say(result.get("message") or result.get("error") or "Response received.")
        action = client.last_action
        if action:
            say(f"{action['title']} · Dataset: {action['dataset']} · Node: {action['node']}")
            say(json.dumps(action["parameters"], ensure_ascii=False, indent=2) or "Default parameters")
            observation = client.conversation.get("operation") or {}
            if observation.get("message"):
                say(observation["message"])
            if observation.get("coverage_notice"):
                say(observation["coverage_notice"])
            if observation.get("links"):
                say("/open shows result and task links. /status refreshes this card.")
            if observation.get("state") == "running":
                say("Already queued or running. Use /status; no duplicate submission is needed.")
            elif action.get("execution"):
                say(f"Task: {action['execution']['status']} · /status for progress")
            elif action.get("is_current") is False:
                say("Action is stale. Ask to prepare it again.")
            elif action["can_execute"]:
                say(
                    "/confirm again explicitly starts a new run after reviewing the existing result or attempt."
                    if observation.get("requires_rerun")
                    else "/confirm to submit this operation. Nothing has run yet."
                )
                say("Describe parameter changes in chat to prepare a revised card before confirming.")
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
                if message in {"/exit", "/quit", "exit", "quit"}:
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
                elif message in {"/chats", "/chats next"}:
                    if message == "/chats next" and not chat_cursor:
                        say("No more conversations. Use /chats to refresh.")
                        continue
                    page = client.conversations(chat_cursor if message == "/chats next" else None)
                    chats, chat_cursor = page["conversations"], page["next_cursor"]
                    for number, item in enumerate(chats, 1):
                        say(
                            f"{number}. {item['title']} · {item['context']['type']} · {item['conversation_id']}"
                            + ("" if item["available"] else " · Context changed")
                        )
                    say(
                        "/restore <number or conversation-id> opens a conversation."
                        + (" /chats next loads more." if chat_cursor else "")
                    )
                elif message == "/preferences" or message.startswith("/preferences "):
                    current = client.preferences()
                    parts = message.split(maxsplit=2)
                    if len(parts) == 1:
                        say(json.dumps(current["settings"], ensure_ascii=False, indent=2))
                        say(
                            "/preferences language auto|zh|en · detail concise|balanced|detailed\n"
                            "/preferences favorites viewer.prepare,curation.quality · on|off|reset\n"
                            "Use /preferences favorites none to clear favorites. "
                            "Preferences affect replies and suggestions, never execution or permissions."
                        )
                    elif parts[1:] == ["reset"]:
                        client.clear_preferences(current["revision"])
                        say("Preferences cleared.")
                    else:
                        settings = current["settings"]
                        if parts[1:] in (["on"], ["off"]):
                            settings["enabled"] = parts[1] == "on"
                        elif len(parts) == 3 and parts[1] == "language" and parts[2] in ("auto", "zh", "en"):
                            settings["language"] = parts[2]
                        elif (
                            len(parts) == 3
                            and parts[1] == "detail"
                            and parts[2] in ("concise", "balanced", "detailed")
                        ):
                            settings["detail"] = parts[2]
                        elif len(parts) == 3 and parts[1] == "favorites":
                            settings["favorite_operators"] = [] if parts[2] == "none" else parts[2].split(",")
                        else:
                            raise ClientError("Unsupported preference. Use /preferences for options.")
                        client.save_preferences(current["revision"], settings)
                        say("Preferences saved. Shared with the website in this environment.")
                elif message.startswith("/restore "):
                    identity = message.split(maxsplit=1)[1]
                    if identity.isdigit():
                        if not 1 <= int(identity) <= len(chats):
                            raise ClientError("Choose a conversation number from /chats.")
                        selected = chats[int(identity) - 1]
                        if not selected["available"]:
                            raise ClientError("Conversation context changed. Start a new chat.")
                        identity = selected["conversation_id"]
                    value = client.restore(identity)
                    say("Conversation restored.")
                    for item in value["history"]:
                        say(item["role"] + ": " + item["content"])
                    display({"message": "Use /resume if a response is pending."})
                elif message == "/resume":
                    with console.status("Waiting for assistant…"):
                        result = client.resume()
                    display(result)
                elif message in {"/confirm", "/confirm again"}:
                    receipt = client.confirm(rerun=message == "/confirm again")
                    say(
                        f"{'Existing task' if receipt.get('reused') else 'Task submitted'}: {receipt.get('run_id') or receipt.get('job_id')} · {receipt['status']}\n/status for progress"
                    )
                elif message == "/open":
                    for link in client.links():
                        say(link["title"] + ": " + link["url"])
                elif message == "/web":
                    for link in client.web_links():
                        say(link["title"] + ": " + link["url"])
                elif message == "/status":
                    observation = client.status()
                    say(f"{observation.get('state', 'task')} · {observation['status']}")
                    say(observation.get("message", ""))
                    if observation.get("coverage_notice"):
                        say(observation["coverage_notice"])
                    for step in observation.get("steps", []):
                        say(f"{step['step_id']}: {step['status']}")
                    say("/open shows result and task links. Describe parameter changes in chat.")
                elif message.startswith("/"):
                    say("Unknown command. Use /help.")
                else:
                    with console.status("Assistant is working…"):
                        result = client.chat(message)
                    display(result)
            except KeyboardInterrupt:
                say(
                    "Input interrupted. /exit to quit; /resume or /status to continue. Server work continues."
                )
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
