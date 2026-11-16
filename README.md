# openai-image-mcp

A very small [MCP](https://modelcontextprotocol.io) server that generates and edits images with
OpenAI's image models: `gpt-image-2.6-flare` for generation and `gpt-image-2.6-sunburst` for edits.

It is one Python file, `image_server.py`. [`uv`](https://docs.astral.sh/uv/) installs its dependencies
automatically from the inline script metadata, so you don't need a virtualenv or `pip install`.

## Tools

| Tool | What it does |
|---|---|
| `generate_image(prompt, size, quality, background, output_format, n, complexity, filename, model)` | Text → image(s) |
| `edit_image(prompt, image_paths, mask_path, ...)` | Edit one image, or build a new one from several reference images |

- `output_format`: `png`, `jpeg`, `webp`, `svg` (vector, generation only, `n=1`) or `gif` (animated)
- `quality`: `auto`, `low`, `medium`, `high`, `xhigh`, `max`. Use `low` for drafts.
- `complexity`: `low`, `medium`, `high` (the model's default is `high`)

Images are saved to `output/` next to the script (change this with `IMAGE_OUTPUT_DIR`). Each call
returns the saved file paths and a 512px preview of each raster image, so the agent can see the result.

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Copy `.env.example` to `.env` and set `OPENAI_API_KEY=sk-...`
   (or pass the variable in your MCP client config).
3. Register the server with your MCP client. The command is:

   ```
   uv run --script /path/to/image_server.py
   ```

### Claude Code

```bash
claude mcp add --scope user openai-image -- uv run --script /path/to/image_server.py
```

### Claude Desktop

Add this to `mcpServers` in `claude_desktop_config.json` (**Settings → Developer → Edit Config**),
then restart Claude Desktop:

```json
{
  "mcpServers": {
    "openai-image": {
      "command": "uv",
      "args": ["run", "--script", "/path/to/image_server.py"]
    }
  }
}
```

### VS Code (GitHub Copilot agent mode)

Add this to your user `mcp.json` (Command Palette → **MCP: Open User Configuration**) or to
`.vscode/mcp.json`:

```json
{
  "servers": {
    "openai-image": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--script", "/path/to/image_server.py"]
    }
  }
}
```

If the client can't find `uv`, use the full path to the `uv` executable as `command`.

## Configuration

| Variable | Default |
|---|---|
| `OPENAI_API_KEY` | required |
| `IMAGE_MODEL` | `gpt-image-2.6-flare` |
| `IMAGE_EDIT_MODEL` | `gpt-image-2.6-sunburst` |
| `IMAGE_SVG_MODEL` | `gpt-5.5` (text model that writes the SVG) |
| `IMAGE_OUTPUT_DIR` | `./output` next to the script |

## License

MIT
