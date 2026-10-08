"""The dimos external-blueprint registration, as dimos resolves it
(dimos/robot/external_blueprints.py at dc80d89), without importing dimos."""

import re
from importlib.metadata import distribution, entry_points

# external_blueprints.LOCAL_BLUEPRINT_NAME_PATTERN and canonicalize_distribution_namespace
LOCAL_BLUEPRINT_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def canonical(name: str) -> str:  # packaging.utils.canonicalize_name
    return re.sub(r"[-_.]+", "-", name).lower()


def test_entry_point_is_registered_where_dimos_looks():
    eps = [ep for ep in entry_points(group="dimos.blueprints") if ep.dist and ep.dist.metadata["Name"] == "getbody-dimos"]
    assert [(ep.name, ep.value) for ep in eps] == [
        ("unitree-go2-mcp", "getbody_dimos.blueprints.unitree_go2:unitree_go2_mcp")]
    ep = eps[0]
    assert LOCAL_BLUEPRINT_NAME_PATTERN.fullmatch(ep.name)
    # `dimos run <namespace>.<name>`; dimos splits on the first "."
    qualified = f"{canonical(distribution('getbody-dimos').metadata['Name'])}.{ep.name}"
    assert qualified == "getbody-dimos.unitree-go2-mcp"
    assert qualified.partition(".")[0] == "getbody-dimos"
