from lerobot.data_platform.routes.compare import register_compare_routes
from lerobot.data_platform.routes.construction import register_construction_routes
from lerobot.data_platform.routes.context import RouteContext
from lerobot.data_platform.routes.control_plane import (
    register_control_plane_auth_routes,
    register_control_plane_routes,
)
from lerobot.data_platform.routes.embedding import register_embedding_routes
from lerobot.data_platform.routes.lifecycle import register_lifecycle_routes
from lerobot.data_platform.routes.preprocess import register_preprocess_routes
from lerobot.data_platform.routes.tagging import register_tagging_routes
from lerobot.data_platform.routes.tasks import register_task_routes

__all__ = [
    "RouteContext",
    "register_management_routes",
    "register_compare_routes",
    "register_control_plane_auth_routes",
    "register_control_plane_routes",
    "register_construction_routes",
    "register_embedding_routes",
    "register_lifecycle_routes",
    "register_preprocess_routes",
    "register_remote_analysis_routes",
    "register_tagging_routes",
    "register_task_routes",
]
from lerobot.data_platform.routes.analysis import register_remote_analysis_routes
from lerobot.data_platform.routes.management import register_management_routes
