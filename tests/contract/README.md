# dimos MCP contract

`dimos-dc80d89.json` is what the dimos McpServer offers when it runs the
`getbody-dimos.unitree-go2-mcp` blueprint, at:

- dimos commit `dc80d89b89558e9ef3bcccc7949b20aa3e2b4d3b` (2026-10-08), version 0.0.14
- https://github.com/dimensionalOS/dimos/tree/dc80d89b89558e9ef3bcccc7949b20aa3e2b4d3b

It holds the `initialize` result and the `tools/list` result: every tool's
name, `inputSchema`, `description` and `_meta`.

## How it was made

It was derived from the dimos source, not captured from a running dimos.
`tools/derive_contract.py` reads each `@skill` method of the blueprint's
modules with `ast`, rebuilds a stub with the same signature, docstring and
decorator wrappers, and runs the same call dimos makes in
`Module.get_skills()`:

```python
json.dumps(tool(attr).args_schema.model_json_schema())   # langchain_core.tools.tool
```

It then applies `McpServer._handle_tools_list()`'s changes: the description
moves to the tool, the title is dropped, and `_meta` is added for skills with
capabilities or a background lifecycle. It used the versions dimos pins in
its `uv.lock` (langchain-core 1.3.3, pydantic 2.12.5) on Python 3.12, which
is what dimos runs on.

```sh
uv run --no-project -p 3.12 --with langchain-core==1.3.3 --with pydantic==2.12.5 \
    python -I tools/derive_contract.py /path/to/dimos tests/contract/dimos-<sha>.json
```

To move to a newer dimos: check out that commit, run the line above with the
versions in its `uv.lock`, update `PINNED_COMMIT` in `tests/test_contract.py`
and the fake server's docstring, and run the tests.

## Not verified

The contract has not been compared with `dimos mcp list-tools` from a running
dimos. The derivation copies dimos's own code path, but the stubs are not
dimos itself. Run this against a live server to confirm:

```sh
dimos --simulation run getbody-dimos.unitree-go2-mcp --daemon
dimos mcp list-tools > live.json      # then diff against dimos-dc80d89.json's "tools"
```
