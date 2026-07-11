"""Build the ZIP asset used for direct Blender installation and GitHub Releases."""

from pathlib import Path

import update_utils


ROOT = Path(__file__).resolve().parent
version = update_utils.read_version_from_init_text(
    (ROOT / "__init__.py").read_text(encoding="utf-8")
)
destination = ROOT / f"QuickRender-v{update_utils.format_version(version)}.zip"
update_utils.build_release_archive(ROOT, destination, "quick_selected_render_engine")
print(destination)
