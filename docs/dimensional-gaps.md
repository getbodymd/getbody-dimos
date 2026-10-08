# Notes for the Dimensional team (draft, not sent)

getbody-dimos drives a dimos robot only through the MCP server
(`http://127.0.0.1:9990/mcp`) and doesn't change dimos. Two things are missing
over MCP that a remote operator needs. Both are small, and either would
remove a workaround here. Checked against dimos `main` at `dc80d89`
(2026-10-08).

## 1. A single tool that stops all motion

**Today.** Stopping is split across one tool per activity:
`stop_navigation`, `end_exploration`, `stop_patrol`, `stop_following`. On a
kill the bridge calls all of them at once and hopes that covers whatever is
running. Some details make this weaker than it looks:

- `stop_navigation` → `ReplanningAStarPlanner.cancel_goal()` →
  `GlobalPlanner.cancel_goal()` returns early when there is no goal and the
  local planner is idle, so it sends no zero `cmd_vel` in that case. Motion
  from another source (a follow loop, teleop) isn't zeroed by it.
- `GO2Connection.stop_movement()`, which zeroes the base directly, is an `@rpc`
  and not a `@skill`, so MCP clients can't call it.
- A new motion skill added later won't be covered by a hard-coded list of stop
  tools.

**Ask.** A `stop_all` skill (name up to you) that:

1. cancels every motion holder (anything holding `CAP_MOVEMENT`, plus the
   planner goal),
2. publishes a zero `Twist` on `cmd_vel` and calls the connection's
   `stop_movement()`,
3. needs no capability itself, so it is never refused as busy,
4. returns quickly and reports what it stopped (e.g. JSON
   `{"cancelled": ["move_to", "begin_exploration"], "zeroed": true}`).

## 2. A read-only pose / velocity tool

**Today.** No MCP tool returns the robot's pose or velocity. The bridge reads
the pose out of `move_to`'s text result ("Robot is at x=.. y=.. heading=..deg"),
and to confirm a kill really stopped the robot it has to subscribe to
`dimos/odom/geometry_msgs.PoseStamped` on zenoh (or `/odom#...` on LCM) and
decode it with `dimos-lcm`. That reaches past the MCP boundary and depends on
transport details.

**Ask.** A `get_pose` skill (instant, no capability) returning JSON such as:

```json
{"frame": "world", "x": 1.23, "y": -0.40, "heading_deg": 87.0,
 "linear_speed": 0.00, "angular_speed": 0.00, "stamp": 1760000000.123}
```

`linear_speed` / `angular_speed` (from odometry or the last `cmd_vel`) would
let any MCP client confirm a stop without touching the bus.

## Smaller things noticed along the way

- `observe` returns images as OpenAI-style
  `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}`.
  MCP clients expect `{"type": "image", "data": "...", "mimeType": "image/jpeg"}`,
  which is why some clients can't decode the frame. Returning the MCP form
  from `McpServer` (and converting for the LangChain agent in `McpClient`)
  would fix it for every client.
- "Tool not found" and busy-capability refusals come back as ordinary
  results without `isError: true`, so clients have to match on the text.
- `AGENTS.md` still shows `dimos mcp call move --arg x=0.5 --arg duration=2.0`;
  there is no `move` tool in `unitree-go2-agentic` any more (`move_to` replaced it).
- `move_to` blocks for up to ~100 s. A `lifecycle="background"` variant that
  returns at once and reports arrival through the tool stream would suit
  remote callers with short reply deadlines.
- `move_to` doesn't declare `uses=[CAP_MOVEMENT]`, so exploration or patrol
  can start while it runs. `navigate_with_text` declares it but releases it
  on return while the robot keeps going (there's already a TODO about this
  in `navigation.py`).
