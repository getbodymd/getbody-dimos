"""unitree-go2 with the dimos MCP server and the skills the bridge maps, and
no LLM agent or web chat input, so a GetBody renter is the only one driving.

Loaded by dimos, never by the bridge:

    dimos --simulation run getbody-dimos.unitree-go2-mcp

It is unitree-go2-agentic without McpClient (the LLM agent), WebInput,
SpeakSkill, PersonFollowSkillContainer and PerceiveLoopSkill.
"""

from dimos.agents.mcp.mcp_server import McpServer
from dimos.agents.skills.navigation import NavigationSkillContainer
from dimos.agents.skills.observe_skill import ObserveSkill
from dimos.core.coordination.blueprints import autoconnect
from dimos.perception.experimental.spatial_perception import SpatialMemory
from dimos.robot.unitree.go2.blueprints.smart.unitree_go2 import unitree_go2
from dimos.robot.unitree.unitree_skill_container import UnitreeSkillContainer

unitree_go2_mcp = autoconnect(
    unitree_go2,
    SpatialMemory.blueprint(),        # NavigationSkillContainer needs it (tag_location)
    McpServer.blueprint(),
    NavigationSkillContainer.blueprint(),
    ObserveSkill.blueprint(),
    UnitreeSkillContainer.blueprint(),
).global_config(n_workers=8)
