"""Shared authorized operator descriptions for forms and model tools."""

from lerobot.data_platform.operators.presentation import presentation
from lerobot.data_platform.processing.planner import availability


class OperatorCatalog:
    def __init__(self, manager, owner, location_id=None):
        self.manager, self.owner = manager, owner
        self.location = self.node = None
        with manager.store.sessions() as session:
            self.role = manager._actor(session, owner, write=False).role
            if location_id:
                self.location, self.node = manager._location(session, location_id, owner, write=False)
        self.info = manager._info(self.location) if self.location else {}
        self.restricted = manager.store.restricted_dataset_keys(owner) is not None

    def describe(self, spec):
        reason = (
            availability(spec, self.node, self.info, validate_dataset=bool(self.info.get("features")))
            if self.location
            else "Choose a registered dataset location"
        )
        if spec.review_required and self.restricted:
            reason = "Curation review requires unrestricted dataset access"
        if not spec.pipeline_allowed:
            reason = "Use the dedicated authorized operation form"
        if self.role == "viewer":
            reason = "Viewer accounts are read-only"
        return {
            **spec.to_dict(),
            "available": reason is None,
            "reason": reason,
            "presentation": presentation(spec.operator_id),
        }
