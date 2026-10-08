# getbody-dimos

List any DimOS robot on GetBody so AI agents can lease it. Maps its MCP skills
to GetBody commands, with lease-scoped permissions and a hard kill. Unofficial.

> **Unofficial.** This project is not made, endorsed or supported by
> Dimensional Inc. or by GetBody. It talks to a running
> [DimOS](https://github.com/dimensionalOS/dimos) process over its MCP
> endpoint and doesn't modify or include DimOS.
>
> **Status: not yet run against a real DimOS.** Everything is tested
> against a fake DimOS server built from the DimOS source (see
> [Tested / not yet tested](#tested--not-yet-tested)). Don't rely on it with
> real hardware until it has been through GetBody's stand-in checklist on a
> simulator.

## What it is

[GetBody](https://getbody.md) lets AI agents lease robots. An owner lists a
robot by running a *bridge*: a process that holds GetBody's body-interface
WebSocket, carries out renter commands and feed reads, and stops the robot on
a kill.

getbody-dimos is that bridge for robots run by DimOS:

```
GetBody  <--wss-->  getbody-dimos  <--HTTP JSON-RPC-->  dimos McpServer (:9990)  -->  robot / sim
                          |
                          +-- optional: odometry from the dimos bus (zenoh or LCM)
```

- **GetBody side.** GetBody's ready-made bridge (pinned, MIT, under
  [`src/getbody_dimos/vendor/`](src/getbody_dimos/vendor/README.md)) handles
  signing, reconnects, heartbeats, `invocation_id` dedupe, kill and re-arm.
- **DimOS side.** A small MCP client calls dimos tools (`move_to`,
  `observe`, ...) as the config allows. It doesn't import dimos.
- **Allowlist.** Nothing is exposed by default. `config.yaml` names each
  GetBody command, the dimos tool behind it, and the parameter limits. The
  bridge refuses out-of-range or unknown params *before* calling dimos. At
  startup it checks the config against dimos's live tool list and won't start
  if a tool, argument or type doesn't match.
- **Long skills.** Commands that outlast GetBody's 5 s reply limit (e.g.
  `move_to`, which blocks until the robot arrives) reply `"started"`. The
  `task` feed then reports progress and the final outcome. One runs at a
  time, each with a runtime cap.
- **Kill.** The bridge calls every dimos stop tool at once and replies
  `kill_ack` with `halted` as found (see [Safety](#safety)). A running
  background command is reported as `interrupted` with outcome `partial`.
  Every command is refused until a human types `rearm`.
- **Supervisory control only.** No teleop.

## Quickstart (simulation)

DimOS runs on Ubuntu 22.04/24.04 or macOS
([install](https://github.com/dimensionalOS/dimos#installation)). Install
this package into the **same Python environment as dimos**, so dimos can find
the agent-free blueprint:

```sh
git clone <this repo> getbody-dimos
uv pip install -e ./getbody-dimos            # run inside the dimos environment
```

**Terminal 1:** dimos in MuJoCo, with the MCP server on and no LLM agent:

```sh
dimos --simulation run getbody-dimos.unitree-go2-mcp
```

**Terminal 2:** check the mapping, then start GetBody's local stand-in:

```sh
getbody-dimos check --config getbody-dimos/examples/unitree-go2/config.yaml
getbody-dimos plan  --config getbody-dimos/examples/unitree-go2/config.yaml -o plan.json
getbody-dimos standin --plan plan.json
```

**Terminal 3:** the bridge, pointed at the stand-in:

```sh
getbody-dimos run --config getbody-dimos/examples/unitree-go2/config.yaml --url ws://127.0.0.1:8765
```

The stand-in walks through GetBody's pre-listing checklist and asks you to
confirm each step while you watch the sim: heartbeats, every command, a
repeated `invocation_id`, every feed, a kill, and that nothing runs until
`rearm`.

Other commands:

| Command | What it does |
|---|---|
| `getbody-dimos check --config C` | Connects to dimos, checks every mapped tool, argument and type, and lists the stop tools found |
| `getbody-dimos plan --config C -o plan.json` | Writes the stand-in's plan from the config |
| `getbody-dimos schemas --config C` | Prints `command_schemas` and `offered_feeds` for a GetBody listing |
| `getbody-dimos run --config C --url ws://...` | Runs the bridge against the stand-in |
| `getbody-dimos run --config C --body-id N --key agent_key.json` | Runs it against getbody.md, once the body is registered (a separate, later step) |

Add `--log-level DEBUG` before the subcommand for more detail. Kills and
failed stops are logged at `WARNING` and above in every case.

## Configuration

See [`examples/unitree-go2/config.yaml`](examples/unitree-go2/config.yaml)
for a commented example.

```yaml
mcp:
  url: http://127.0.0.1:9990/mcp   # dimos McpServer (GlobalConfig.mcp_port)
  timeout_s: 4.0                   # per call, at most 4.5 (GetBody drops replies after 5 s)
ack_after_s: 1.0                   # background commands reply "started" after this long
pose_regex: '...'                  # optional: named groups x, y, heading, read from tool replies

commands:
  <getbody name>:
    tool: <dimos tool>             # or `builtin: stop` (calls every stop tool; not a kill)
    mode: sync | background        # background: replies "started", runs on, see the task feed
    max_runtime_s: 30              # background only, required: the bridge stops the robot after this
    fixed: {relative: true}        # passed to the tool; the renter can't set or override these
    params:                        # what the renter may set; anything else is refused
      x: {type: number, min: -1.0, max: 1.0, default: 0.0}
      name: {type: string, required: true, max_length: 40, pattern: '[a-z ]+'}
      mode: {type: string, enum: [a, b]}
      flag: {type: boolean, default: false}
    fault_patterns: ["timed out"]  # regexes: a reply matching one is reported as a fault
    example: {x: 0.3}              # used for plan.json; must be within the limits

feeds:
  <getbody name>: {kind: image, tool: observe, max_bytes: 2000000}   # base64 image
  <getbody name>: {kind: number, tool: get_battery_soc}             # {"value", "available"}
  <getbody name>: {kind: json, tool: server_status}                 # parsed JSON
  <getbody name>: {kind: text, tool: current_time}                  # raw text
  <getbody name>: {kind: task}     # the bridge's last background command and last pose
  <getbody name>: {kind: odom}     # pose and speed from odometry; needs the odom section

stop:
  tools: [stop_navigation, end_exploration, stop_patrol]   # all called on a kill or a stop
  required: [stop_navigation]      # halted needs these to return ok (default: all found)
  timeout_s: 2.0

odom:                              # optional: measured halt
  backend: zenoh                   # or lcm (when dimos runs with DIMOS_TRANSPORT=lcm)
  topic: ""                        # default: dimos's odom channel for the backend
  max_speed: 0.05                  # m/s
  max_yaw_rate: 0.1                # rad/s
  window_s: 0.5
  timeout_s: 3.0                   # at most 6
  stale_s: 1.0
  zenoh_connect: []                # e.g. [tcp/127.0.0.1:7447] if dimos uses a router
  zenoh_config: {}                 # extra zenoh settings, key -> value
  lcm_url: null                    # default: $LCM_DEFAULT_URL, else udpm://239.255.76.67:7667?ttl=0
```

The config loader enforces some rules:

- Numbers need `min` and `max`.
- Strings need an `enum`, or both `max_length` and `pattern`.
- Background commands need `max_runtime_s`.
- Examples and defaults must be within their own limits.

### The go2 example

| GetBody command | dimos tool | Limits |
|---|---|---|
| `move` | `move_to(relative=true)` | x, y each within ±1.0 m; runs ≤ 30 s |
| `turn` | `move_to(relative=true, x=0, y=0)` | degrees within ±90; runs ≤ 20 s |
| `stop` | every stop tool | cancels a running move or turn (not a kill) |
| `tag_location` | `tag_location` | name ≤ 40 characters from `[A-Za-z0-9 _-]` |

| Feed | Source |
|---|---|
| `camera` | `observe`, base64 JPEG |
| `task` | the bridge's last move or turn (state, result, last pose) |

Battery (`get_battery_soc`) is in the config but commented out: MuJoCo has no
battery, so dimos always returns `None` in sim.

## Safety

- **Run a blueprint without an LLM agent.** `unitree-go2-agentic` includes an
  LLM agent and a web chat input that can move the robot outside the renter's
  control, even after a kill. Use `getbody-dimos.unitree-go2-mcp` (shipped
  here: `unitree-go2-agentic` minus the agent, web input, speech, person-follow
  and perception loop) or another blueprint with `McpServer` and no
  `McpClient` / `WebInput`.
- **What `halted` means.** dimos has no MCP tool that reports motion.
  - **Without odometry:** `halted: true` means every required stop tool
    returned ok, and `kill_ack.state.verified` is `false`.
  - **With the `odom` section** (`pip install 'getbody-dimos[odom-zenoh]'`):
    `halted` is true only once measured speed and yaw rate drop under the
    limits (`verified: true`). No odometry, stale odometry or a failed check
    all count as **not halted**.
  - In both cases a halt that can't be confirmed is reported as
    `halted: false` and logged at `CRITICAL`.
  - See [docs/dimensional-gaps.md](docs/dimensional-gaps.md) for what dimos
    could add.
- **Stopping is split across tools in dimos.** `stop_navigation` cancels a
  `move_to` goal but not exploration or patrol, which have their own stop
  tools. A kill calls all of them. If you add modules with other motion
  skills, add their stop tools to `stop.tools`.
- **Limits live in the bridge.** Keep them conservative and test at the lowest
  values first. `max_runtime_s` stops the robot if a background command runs
  long.
- **Real hardware.** Have someone at the robot with its physical stop within
  reach for every test, as GetBody's checklist asks.

## Tested / not yet tested

**Tested** (CI: Linux on Python 3.10 to 3.12, and Windows; locally on Windows):

- **Unit tests** for every module, against a fake dimos McpServer.
- **The fake server.** It serves a tool list derived from the dimos source at
  commit [`dc80d89`](https://github.com/dimensionalOS/dimos/tree/dc80d89b89558e9ef3bcccc7949b20aa3e2b4d3b)
  (0.0.14) by running dimos's own schema code path
  ([tests/contract/](tests/contract/README.md)). Its replies follow that
  source: reply texts, in-band refusals, `isError`, image parts and
  capability conflicts.
- **The config against the contract.** Tests fail if the config names a tool,
  argument or type the contract doesn't have.
- **GetBody's own stand-in checklist**, end to end against the fake: all 11
  checks, including dedupe, a kill during a move, and refusal until re-arm.
- **The blueprint, from source only.** Its imports and module composition
  are checked against the dimos source, and its entry-point registration is
  checked the way dimos resolves it.
- **The odometry probe against a real zenoh session.** The session publishes
  LCM-encoded `PoseStamped` on dimos's key, and the probe has to decode it
  and measure speed.
- **CI re-derives the contract** from the pinned dimos commit and fails if it
  has drifted.

**Not yet tested on real dimos.** These need a run with dimos in a simulator:

1. **The tool list.** That `dimos mcp list-tools` matches the derived
   contract.
2. **The blueprint loads and starts.** That `dimos run
   getbody-dimos.unitree-go2-mcp` resolves the entry point, deploys with 8
   workers, and wires `NavigationSkillContainer`'s spatial-memory and
   navigation specs.
3. **The camera.** That `observe` returns a real MuJoCo frame without the
   perception loop. The source shows `GO2Connection` publishes `color_image`
   and `ObserveSkill` subscribes to it, but rendering wasn't exercised.
4. **Odometry.** That dimos's zenoh sessions find the probe's session, that
   the channel is `/odom` at run time, and that the measured speed matches
   the sim.
5. **Real timing.** How long `move_to` takes, how fast `stop_navigation`
   takes effect, and that the robot actually stops when the stop tools
   return ok.
6. **Error text.** How dimos's RPC layer words exceptions raised inside
   module workers (the fake assumes `Error running tool '<name>': <Type>: <message>`).
7. **Concurrent calls.** That stop tools are served while a `move_to` call is
   still blocking.
8. **The LCM odometry backend.** Not run at all (`lcm-dimos-fork` has no
   Windows wheel).
9. **getbody.md itself.** Never connected to; only GetBody's local stand-in
   was used.

## Development

```sh
uv sync                                   # add --extra odom-zenoh on Linux/macOS
uv run pytest                             # unit tests, contract tests, stand-in checklist
uv run ruff check src tests tools
uv run mypy
```

To check against a dimos checkout:

```sh
uv run --no-project -p 3.12 --with langchain-core==1.3.3 --with pydantic==2.12.5 \
    python -I tools/derive_contract.py /path/to/dimos tests/contract/dimos-<sha>.json
python tools/check_blueprint.py /path/to/dimos
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). DimOS is also
Apache-2.0. The vendored GetBody bridge is MIT
([src/getbody_dimos/vendor/LICENSE](src/getbody_dimos/vendor/LICENSE)).
