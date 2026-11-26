import zipfile
from pathlib import Path


def create_zip(source_dir: str, zip_path: str) -> None:
    """
    Create a ZIP archive from all files in source_dir.

    Parameters:
    - source_dir: directory containing files to zip
    - zip_path: path to output ZIP file
    """

    source_dir = Path(source_dir)
    zip_path = Path(zip_path)

    if not source_dir.exists():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in source_dir.rglob("*"):
            if file_path.is_file():
                zf.write(
                    file_path,
                    arcname=file_path.relative_to(source_dir)
                )
