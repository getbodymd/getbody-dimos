# getbody-dimos

List any DimOS robot on GetBody so AI agents can lease it. Maps its MCP skills
to GetBody commands, with lease-scoped permissions and a hard kill. Unofficial.

> **Unofficial.** This project is not made, endorsed or supported by
> Dimensional Inc. or by GetBody. It talks to a running
> [DimOS](https://github.com/dimensionalOS/dimos) process over its MCP
> endpoint and doesn't modify or include DimOS.

## What it does

[GetBody](https://getbody.md) lets AI agents lease robots. A robot is listed
by its owner running a *bridge*: a process that holds GetBody's
body-interface WebSocket, carries out renter commands and feed reads, and
stops the robot on a kill.

getbody-dimos is that bridge for robots run by DimOS:

```
GetBody  <--wss-->  getbody-dimos  <--HTTP JSON-RPC-->  dimos McpServer (:9990)  -->  robot / sim
```

- **GetBody side.** GetBody's ready-made bridge (pinned under
  [`src/getbody_dimos/vendor/`](src/getbody_dimos/vendor/README.md)) handles
  signing, reconnects, heartbeats, `invocation_id` dedupe, kill and re-arm.
- **DimOS side.** A small MCP client calls dimos tools (`move_to`,
  `observe`, ...) as the config allows.
- **Allowlist.** Nothing is exposed by default. `config.yaml` names each
  GetBody command, the dimos tool behind it, and the parameter limits. The
  bridge refuses out-of-range or unknown params *before* calling dimos.
- **Long skills.** Commands that outlast GetBody's 5 s reply limit (e.g.
  `move_to`) reply `"started"` and run in the background. The `task` feed
  reports progress and the final outcome. One runs at a time, each with a
  runtime cap.
- **Kill.** The bridge calls every dimos stop tool at once and replies
  `kill_ack` with `halted` as found (see [Safety](#safety)). It refuses
  every command until a human types `rearm`.
- **Supervisory control only.** No teleop.

## Quickstart (simulation)

DimOS runs on Ubuntu 22.04/24.04 or macOS ([install](https://github.com/dimensionalOS/dimos#installation)).
Install this package into the same Python environment as dimos so dimos can
find the agent-free blueprint:

```sh
git clone <this repo> getbody-dimos && cd getbody-dimos
uv pip install -e .            # in the dimos environment
```

Terminal 1: dimos in MuJoCo, MCP server on, no LLM agent:

```sh
dimos --simulation run getbody-dimos.unitree-go2-mcp
```

Terminal 2: check the mapping, then start GetBody's local stand-in:

```sh
getbody-dimos check --config examples/unitree-go2/config.yaml
getbody-dimos plan  --config examples/unitree-go2/config.yaml -o plan.json
getbody-dimos standin --plan plan.json
```

Terminal 3: the bridge, pointed at the stand-in:

```sh
getbody-dimos run --config examples/unitree-go2/config.yaml --url ws://127.0.0.1:8765
```

The stand-in walks through GetBody's pre-listing checklist and asks you to
confirm each step while you watch the sim: heartbeats, every command, a
repeated `invocation_id`, every feed, a kill, and that nothing runs until
`rearm`.

`getbody-dimos schemas --config ...` prints the `command_schemas` and
`offered_feeds` for a listing. Registering on getbody.md (`run --body-id N
--key agent_key.json`) is a separate, later step.

## The go2 example

| GetBody command | dimos tool | Limits |
|---|---|---|
| `move` | `move_to(relative=true)` | x, y each within ±1.0 m; runs ≤ 30 s |
| `turn` | `move_to(relative=true, x=0, y=0)` | degrees within ±90; runs ≤ 20 s |
| `stop` | every stop tool | cancels a running move/turn (not a kill) |
| `tag_location` | `tag_location` | name ≤ 40 chars of `[A-Za-z0-9 _-]` |

| Feed | Source |
|---|---|
| `camera` | `observe`, base64 JPEG |
| `task` | the bridge's last move/turn (state, result, last pose) |

Battery (`get_battery_soc`) is in the config but commented out: MuJoCo has no
battery, so it always reads empty in sim.

## Safety

- **Run a blueprint without an LLM agent.** `unitree-go2-agentic` includes an
  LLM agent and a web chat input that can move the robot outside the renter's
  control, even after a kill. Use `getbody-dimos.unitree-go2-mcp` (shipped
  here) or another blueprint with `McpServer` and no `McpClient` / `WebInput`.
- **What `halted` means.** dimos has no MCP tool that reports motion. By
  default `halted: true` means every required stop tool returned ok, and
  `kill_ack.state.verified` is `false`. Enable the `odom` section (`pip
  install 'getbody-dimos[odom-zenoh]'`) to have `halted` reflect measured
  odometry speed instead (`verified: true`). See
  [docs/dimensional-gaps.md](docs/dimensional-gaps.md).
- **Limits live in the bridge.** Keep them conservative and test at the lowest
  values first. `max_runtime_s` stops the robot if a background command runs
  long.
- **Real hardware.** Have someone at the robot with its physical stop within
  reach for every test, as GetBody's checklist asks.

## Development

```sh
uv sync
uv run pytest     # unit tests + GetBody's stand-in checklist against a fake dimos MCP server
uv run ruff check src tests
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The vendored GetBody
bridge's license is recorded in
[src/getbody_dimos/vendor/README.md](src/getbody_dimos/vendor/README.md).
