"""Subprocess fixture for checkpoint recovery; no platform or provider calls."""

import json
import os
import sys
from typing import TypedDict

import pymysql
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from experiments.assistant_langgraph.checkpointer import FencedMySQLSaver
from experiments.assistant_langgraph.repository import ExecutionRepository


class State(TypedDict, total=False):
    context: str
    answer: str


def main():
    database, execution_id, operation = sys.argv[1:]
    if not database.startswith("assistant_graph_test_") or not database[21:].isalnum():
        raise ValueError("Only generated test databases are supported")

    def connect():
        return pymysql.connect(
            unix_socket=os.environ["ASSISTANT_TEST_MYSQL_SOCKET"],
            user="root",
            database=database,
            autocommit=True,
        )

    repo = ExecutionRepository(connect)
    lease = repo.claim(execution_id, f"process-{os.getpid()}")
    if lease is None:
        raise RuntimeError("Execution already owned")
    graph = StateGraph(State)
    graph.add_node("context", lambda state: {"context": "合成数据 S3"})
    graph.add_node("pause", lambda state: {"answer": interrupt({"context": state["context"]})})
    graph.add_edge(START, "context")
    graph.add_edge("context", "pause")
    graph.add_edge("pause", END)
    with connect() as connection:
        app = graph.compile(checkpointer=FencedMySQLSaver(connection, lease))
        config = {"configurable": {"thread_id": execution_id}}
        result = app.invoke({} if operation == "start" else Command(resume="receipt-only"), config)
        public = {key: value for key, value in result.items() if key != "__interrupt__"}
        public["paused"] = bool(result.get("__interrupt__"))
    if public["paused"]:
        repo.release(lease)
    else:
        repo.finish(lease, public)
    print(json.dumps(public, ensure_ascii=False))


if __name__ == "__main__":
    main()
