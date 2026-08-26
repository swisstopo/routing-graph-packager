import zipfile
from pathlib import Path
from typing import Set


def make_package_path(base_dir: Path, name: str, provider: str) -> Path:
    """
    Returns the ZIP file name from DATA_DIR, provider and dataset name.

    :param base_dir: The DATA_DIR env variable
    :param name: The dataset name, e.g. moldavia
    :param provider: The provider's name, e.g. osm

    :returns: The full path to the data package
    """
    file_name = "_".join([provider, name])

    # also create a folder with the same name
    out_dir = base_dir.joinpath(file_name)
    out_dir.mkdir(parents=True)

    return out_dir.joinpath(file_name + ".zip").resolve()


def make_zip(source_paths: Set[Path], parent_path: Path, out_fp: str):
    """
    ZIPs the input paths.

    :param source_paths: set of paths which need zipping.
    :param parent_path: the valhalla_tiles dir, for the file's arcname.
    :param out_fp: full path to the resulting Zip file.
    """
    with zipfile.ZipFile(out_fp, "w", zipfile.ZIP_DEFLATED) as archive:
        for p in source_paths:
            archive.write(p, "valhalla_tiles/" + str(p.relative_to(parent_path)))
