from lerobot.data_platform.routes.account_passwords import register_account_password_routes
from lerobot.data_platform.routes.ai_assistant import register_ai_assistant_routes
from lerobot.data_platform.routes.caption_reads import register_caption_read_routes
from lerobot.data_platform.routes.compare import register_compare_routes
from lerobot.data_platform.routes.console_datasets import register_console_dataset_routes
from lerobot.data_platform.routes.console_jobs import register_console_job_routes
from lerobot.data_platform.routes.construction import register_construction_routes
from lerobot.data_platform.routes.context import RouteContext
from lerobot.data_platform.routes.control_plane import (
    register_control_plane_auth_routes,
    register_control_plane_routes,
)
from lerobot.data_platform.routes.curation import register_curation_routes
from lerobot.data_platform.routes.dataset_results import register_dataset_result_routes
from lerobot.data_platform.routes.dataset_scans import register_dataset_scan_routes
from lerobot.data_platform.routes.embedding import register_embedding_routes
from lerobot.data_platform.routes.episode_deletion_requests import register_episode_deletion_request_routes
from lerobot.data_platform.routes.inspect import register_inspect_routes
from lerobot.data_platform.routes.lifecycle import register_lifecycle_routes
from lerobot.data_platform.routes.mcap_import import register_mcap_import_routes
from lerobot.data_platform.routes.preprocess import register_preprocess_routes
from lerobot.data_platform.routes.source_cleanup import register_source_cleanup_routes
from lerobot.data_platform.routes.strategies import register_strategy_routes
from lerobot.data_platform.routes.tagging import register_tagging_routes
from lerobot.data_platform.routes.tasks import register_task_routes
from lerobot.data_platform.routes.temporal_caption import register_temporal_caption_routes

__all__ = [
    "register_strategy_routes",
    "register_ai_assistant_routes",
    "register_console_dataset_routes",
    "register_console_job_routes",
    "register_source_cleanup_routes",
    "register_caption_read_routes",
    "register_curation_routes",
    "register_dataset_result_routes",
    "register_dataset_scan_routes",
    "register_account_password_routes",
    "RouteContext",
    "register_processing_routes",
    "register_management_routes",
    "register_compare_routes",
    "register_control_plane_auth_routes",
    "register_control_plane_routes",
    "register_construction_routes",
    "register_embedding_routes",
    "register_episode_deletion_request_routes",
    "register_lifecycle_routes",
    "register_mcap_import_routes",
    "register_inspect_routes",
    "register_preprocess_routes",
    "register_remote_analysis_routes",
    "register_tagging_routes",
    "register_task_routes",
    "register_temporal_caption_routes",
]
from lerobot.data_platform.routes.analysis import register_remote_analysis_routes
from lerobot.data_platform.routes.management import register_management_routes
from lerobot.data_platform.routes.processing import register_processing_routes
