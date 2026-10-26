"""Reading archive references never changes computation state or loads cloud data in lists."""

import pytest

from lerobot.data_platform.artifact_store import ArtifactUnavailableError
from lerobot.data_platform.report_archive import hydrate_result, reference
from tests.datasets.test_artifact_store import archive, source  # noqa: F401


def test_inline_and_archived_report_compatibility(archive, tmp_path):  # noqa: F811
    inline = {"precompute": {"rows": [1, 2]}, "output_dir": "/unchanged"}
    assert hydrate_result(inline) is inline
    ref = archive.enqueue(source(tmp_path, inline["precompute"]), "reports")
    archive.flush()
    result = {**inline, "precompute": reference(ref)}
    assert hydrate_result(result, archive) == inline
    assert "_oss_artifact" in result["precompute"]
    archive.transport.offline = True
    for path in (archive.root / "cache").iterdir():
        path.unlink()
    with pytest.raises(ArtifactUnavailableError):
        hydrate_result(result, archive)
    assert "_oss_artifact" in result["precompute"]
