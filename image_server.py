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
import io
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image
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


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "image"


def _save_and_preview(result, name_hint: str, output_format: str) -> list:
    """Save every returned image to disk; return a text summary plus small previews."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = "jpg" if output_format == "jpeg" else output_format
    paths, previews = [], []
    for i, item in enumerate(result.data):
        raw = base64.b64decode(item.b64_json)
        suffix = f"-{i + 1}" if len(result.data) > 1 else ""
        path = OUTPUT_DIR / f"{stamp}-{_slug(name_hint)}{suffix}.{ext}"
        path.write_bytes(raw)
        paths.append(str(path))

        if ext == "svg":
            continue  # vector output: no raster preview
        img = PILImage.open(io.BytesIO(raw))  # for gif this is the first frame
        img.thumbnail((PREVIEW_PX, PREVIEW_PX))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
        previews.append(Image(data=buf.getvalue(), format="jpeg"))

    summary = "Saved:\n" + "\n".join(paths)
    revised = [d.revised_prompt for d in result.data if getattr(d, "revised_prompt", None)]
    if revised:
        summary += "\nRevised prompt: " + revised[0]
    return [summary, *previews]


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
def generate_image(
    prompt: str,
    size: str = "auto",
    quality: str = "auto",
    background: str = "auto",
    output_format: str = "png",
    n: int = 1,
    complexity: str | None = None,
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
        filename: Optional short name used in the saved file name.
        model: Override the model (default from IMAGE_MODEL).

    Returns the saved file paths and small previews of each image.
    """
    result = client().images.generate(
        model=model or DEFAULT_MODEL,
        prompt=prompt,
        **_common(size, quality, background, output_format, n, complexity),
    )
    return _save_and_preview(result, filename or prompt, output_format)


@mcp.tool()
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
    filename: str | None = None,
    model: str | None = None,
) -> list:
    """Edit an image, or compose a new one from several reference images, guided by a prompt.

    Args:
        prompt: What to change or create.
        image_paths: One or more local image file paths (png/jpg/webp) used as input/references.
        mask_path: Optional PNG with alpha channel; transparent areas mark where to edit.
            Applies to the first image and must match its size.
        size, quality, background, output_format, n, complexity, filename, model: as in
            generate_image (model defaults to IMAGE_EDIT_MODEL; svg output is not supported here).

    Returns the saved file paths and small previews of each image.
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
    return _save_and_preview(result, filename or prompt, output_format)


if __name__ == "__main__":
    mcp.run()
