"""Root-graph checkpoints sharing the control database's lease transaction.

Storing complete bounded checkpoints avoids a second migration/transaction system.
This saver deliberately has no unscoped history, subgraph or deletion interface.
"""

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_id,
    get_serializable_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import select

from lerobot.data_platform.ai_tools.models import AssistantCheckpoint, AssistantCheckpointWrite


class RuntimeSaver(BaseCheckpointSaver):
    def __init__(self, repository, lease):
        super().__init__(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        self.repository, self.lease = repository, lease

    def _scope(self, config):
        values = config.get("configurable", {})
        if values.get("thread_id") != self.lease.request_id or values.get("checkpoint_ns", ""):
            raise ValueError("Checkpoint must belong to this execution's root graph")

    def _config(self, checkpoint_id):
        return {
            "configurable": {
                "thread_id": self.lease.request_id,
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            }
        }

    def _dump(self, value):
        kind, data = self.serde.dumps_typed(value)
        if len(data) > 1048576:
            raise ValueError("Assistant checkpoint exceeds limit")
        return kind, data

    def get_tuple(self, config):
        self._scope(config)
        with self.repository.transaction(self.lease) as session:
            query = select(AssistantCheckpoint).where(AssistantCheckpoint.request_id == self.lease.request_id)
            checkpoint_id = get_checkpoint_id(config)
            if checkpoint_id:
                query = query.where(AssistantCheckpoint.checkpoint_id == checkpoint_id)
            row = session.scalar(query.order_by(AssistantCheckpoint.checkpoint_id.desc()).limit(1))
            if row is None:
                return None
            writes = session.scalars(
                select(AssistantCheckpointWrite)
                .where(
                    AssistantCheckpointWrite.request_id == self.lease.request_id,
                    AssistantCheckpointWrite.checkpoint_id == row.checkpoint_id,
                )
                .order_by(AssistantCheckpointWrite.task_id, AssistantCheckpointWrite.idx)
            ).all()
            return CheckpointTuple(
                config=self._config(row.checkpoint_id),
                checkpoint=self.serde.loads_typed((row.kind, row.data)),
                metadata=row.details,
                parent_config=self._config(row.parent_id) if row.parent_id else None,
                pending_writes=[
                    (write.task_id, write.channel, self.serde.loads_typed((write.kind, write.data)))
                    for write in writes
                ],
            )

    def put(self, config, checkpoint, metadata, new_versions):
        self._scope(config)
        kind, data = self._dump(checkpoint)
        with self.repository.transaction(self.lease) as session:
            session.merge(
                AssistantCheckpoint(
                    request_id=self.lease.request_id,
                    checkpoint_id=checkpoint["id"],
                    parent_id=get_checkpoint_id(config),
                    kind=kind,
                    data=data,
                    details=get_serializable_checkpoint_metadata(config, metadata),
                )
            )
        return self._config(checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        self._scope(config)
        with self.repository.transaction(self.lease) as session:
            for index, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, index)
                key = (self.lease.request_id, get_checkpoint_id(config), task_id, idx)
                previous = session.get(AssistantCheckpointWrite, key)
                if previous is not None and idx >= 0:
                    continue
                kind, data = self._dump(value)
                session.merge(
                    AssistantCheckpointWrite(
                        request_id=key[0],
                        checkpoint_id=key[1],
                        task_id=key[2],
                        idx=idx,
                        channel=channel,
                        kind=kind,
                        data=data,
                    )
                )
