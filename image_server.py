# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=1.10,<2",
#     "openai>=1.80",
#     "pillow>=10",
#     "python-dotenv>=1.0",
# ]
# ///
"""openai-image-mcp: a tiny MCP server that generates and edits images with OpenAI's image models.

Config (env vars or a .env file next to this script):
  OPENAI_API_KEY        required
  IMAGE_MODEL           default model (default: gpt-image-2.6-flare)
  IMAGE_EDIT_MODEL      default model for edits (default: gpt-image-2.6-sunburst)
  IMAGE_SVG_MODEL       text model that writes the SVG for output_format="svg" (default: gpt-5.5)
  IMAGE_OUTPUT_DIR      where images are saved (default: ./output next to this script)
"""

import base64
import functools
import io
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import anyio
from dotenv import load_dotenv
from mcp import types
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.resources import FileResource
from openai import OpenAI
from PIL import Image as PILImage

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

DEFAULT_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2.6-flare")
DEFAULT_EDIT_MODEL = os.getenv("IMAGE_EDIT_MODEL", "gpt-image-2.6-sunburst")
SVG_MODEL = os.getenv("IMAGE_SVG_MODEL", "gpt-5.5")
OUTPUT_DIR = Path(os.getenv("IMAGE_OUTPUT_DIR", HERE / "output")).expanduser()
PREVIEW_PX = 512

mcp = FastMCP("openai-image-mcp")
_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError(f"OPENAI_API_KEY is not set. Add it to {HERE / '.env'}.")
        _client = OpenAI()
    return _client


def threaded(fn):
    """Run a blocking tool in a worker thread.

    FastMCP calls plain (sync) tools directly on the event loop, so one slow image call
    blocks the whole server: parallel calls queue up and hit the client's timeout, and the
    server can't even answer pings meanwhile. Wrapping the tool keeps its signature and
    docstring but lets calls run side by side.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return wrapper


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "image"


MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp",
        "gif": "image/gif", "svg": "image/svg+xml"}


def _target_paths(output_path: str | None, name_hint: str, ext: str, count: int) -> list[Path]:
    """Work out where each image goes.

    No output_path: OUTPUT_DIR/<timestamp>-<slug>.<ext>
    A directory (existing, trailing slash, or no file extension): same name, in that directory.
    A file path: used as given (extension corrected to the real format); -1, -2... when count > 1.
    """
    numbered = lambda i: f"-{i + 1}" if count > 1 else ""
    p = Path(output_path).expanduser().resolve() if output_path else None
    if p is None or p.is_dir() or output_path.endswith(("/", "\\")) or not p.suffix:
        folder = p or OUTPUT_DIR
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return [folder / f"{stamp}-{_slug(name_hint)}{numbered(i)}.{ext}" for i in range(count)]
    return [p.with_name(f"{p.stem}{numbered(i)}.{ext}") for i in range(count)]


def _publish(path: Path, ext: str) -> types.ResourceLink:
    """Expose a saved file as an MCP resource and return a link to it."""
    uri = f"image://{quote(path.name)}"
    mcp.add_resource(FileResource(uri=uri, path=path, name=path.name,
                                  mime_type=MIME[ext], is_binary=True))
    return types.ResourceLink(type="resource_link", uri=uri, name=path.name,
                              mimeType=MIME[ext], size=path.stat().st_size,
                              description=str(path))


def _save_and_preview(result, name_hint: str, output_format: str, output_path: str | None) -> list:
    """Save every returned image; return a summary, resource links and small previews."""
    ext = "jpg" if output_format == "jpeg" else output_format
    paths = _target_paths(output_path, name_hint, ext, len(result.data))
    links, previews = [], []
    for item, path in zip(result.data, paths):
        raw = base64.b64decode(item.b64_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        links.append(_publish(path, ext))

        if ext == "svg":
            continue  # vector output: no raster preview
        img = PILImage.open(io.BytesIO(raw))  # for gif this is the first frame
        img.thumbnail((PREVIEW_PX, PREVIEW_PX))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
        previews.append(Image(data=buf.getvalue(), format="jpeg"))

    summary = "Saved:\n" + "\n".join(map(str, paths))
    revised = [d.revised_prompt for d in result.data if getattr(d, "revised_prompt", None)]
    if revised:
        summary += "\nRevised prompt: " + revised[0]
    return [summary, *links, *previews]


def _common(size, quality, background, output_format, n, complexity):
    args = {"size": size, "quality": quality, "background": background,
            "output_format": output_format, "n": n}
    args = {k: v for k, v in args.items() if v is not None}
    # newer parameters go via extra_body so older SDK versions don't reject them
    extra = {}
    if complexity:
        extra["complexity"] = complexity
    if output_format == "svg":
        extra["svg"] = {"model": SVG_MODEL}  # the image model draws a reference, this model writes the SVG
    if extra:
        args["extra_body"] = extra
    return args


@mcp.tool()
@threaded
def generate_image(
    prompt: str,
    size: str = "auto",
    quality: str = "auto",
    background: str = "auto",
    output_format: str = "png",
    n: int = 1,
    complexity: str | None = None,
    output_path: str | None = None,
    filename: str | None = None,
    model: str | None = None,
) -> list:
    """Generate image(s) from a text prompt with OpenAI's image model and save them to disk.

    Args:
        prompt: Detailed description of the desired image.
        size: "auto", "1024x1024", "1536x1024" (landscape), "1024x1536" (portrait), or custom
            WIDTHxHEIGHT (multiples of 16, aspect between 1:3 and 3:1, max edge 3840).
        quality: "auto", "low", "medium", "high", "xhigh" or "max". Low is fastest/cheapest.
        background: "auto", "opaque" or "transparent" (transparent needs png or webp).
        output_format: "png", "jpeg", "webp", "svg" (vector, n must be 1, no preview)
            or "gif" (animated).
        n: Number of images (1-8).
        complexity: "low", "medium" or "high" (model default: high). Lower is simpler/faster.
        output_path: Where to save, ideally an absolute path. A file path (e.g. the spot in your
            project where the image belongs) is used as given and overwritten if it exists; a
            directory gets an auto-named file. Default: the server's output folder.
        filename: Optional short name used in the saved file name.
        model: Override the model (default from IMAGE_MODEL).

    Returns the saved file paths, an image:// resource link per file and small previews.
    """
    result = client().images.generate(
        model=model or DEFAULT_MODEL,
        prompt=prompt,
        **_common(size, quality, background, output_format, n, complexity),
    )
    return _save_and_preview(result, filename or prompt, output_format, output_path)


@mcp.tool()
@threaded
def edit_image(
    prompt: str,
    image_paths: list[str],
    mask_path: str | None = None,
    size: str = "auto",
    quality: str = "auto",
    background: str = "auto",
    output_format: str = "png",
    n: int = 1,
    complexity: str | None = None,
    output_path: str | None = None,
    filename: str | None = None,
    model: str | None = None,
) -> list:
    """Edit an image, or compose a new one from several reference images, guided by a prompt.

    Args:
        prompt: What to change or create.
        image_paths: One or more local image file paths (png/jpg/webp) used as input/references.
        mask_path: Optional PNG with alpha channel; transparent areas mark where to edit.
            Applies to the first image and must match its size.
        size, quality, background, output_format, n, complexity, output_path, filename, model: as in
            generate_image (model defaults to IMAGE_EDIT_MODEL; svg output is not supported here).

    Returns the saved file paths, an image:// resource link per file and small previews.
    """
    files = [open(Path(p).expanduser(), "rb") for p in image_paths]
    mask = open(Path(mask_path).expanduser(), "rb") if mask_path else None
    try:
        extra = {"mask": mask} if mask else {}
        result = client().images.edit(
            model=model or DEFAULT_EDIT_MODEL,
            prompt=prompt,
            image=files if len(files) > 1 else files[0],
            **extra,
            **_common(size, quality, background, output_format, n, complexity),
        )
    finally:
        for f in files:
            f.close()
        if mask:
            mask.close()
    return _save_and_preview(result, filename or prompt, output_format, output_path)


if __name__ == "__main__":
    mcp.run()
