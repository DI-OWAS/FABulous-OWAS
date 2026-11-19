"""Fold a fabric configured with an emulation bitstream into a flat netlist.

Yosys propagates the constant `EMULATION` configuration, so the netlist keeps
only the logic the user design uses: no configuration storage and no loops
through unused routing.
"""

from itertools import chain
from pathlib import Path

from loguru import logger

from fabulous.fabric_definition.fabric import Fabric
from fabulous.fabric_definition.supertile import SuperTile
from fabulous.fabric_definition.tile import Tile
from fabulous.fabulous_settings import get_context
from fabulous.tools.yosys import YosysTool

_TILE_RTL_SUFFIXES = ("", "_switch_matrix", "_ConfigMem")


def _tile_rtl(tile: Tile | SuperTile) -> list[Path]:
    """Return the generated RTL of one tile or supertile.

    The RTL lies next to the tile CSV or, for projects with several matrix
    variants per tile, next to the selected matrix file.

    Parameters
    ----------
    tile : Tile | SuperTile
        Tile whose generated RTL is collected.

    Returns
    -------
    list[Path]
        `<name>.v` plus its switch matrix and config memory RTL if present.

    Raises
    ------
    FileNotFoundError
        If neither directory holds `<name>.v`.
    ValueError
        If both directories hold a `<name>.v`.
    """
    search_dirs = [tile.tileDir.parent]
    if tile.switch_matrix is not None:
        search_dirs.append(tile.switch_matrix.matrix_file.parent)
    search_dirs = list(dict.fromkeys(d.resolve() for d in search_dirs))

    match [d for d in search_dirs if (d / f"{tile.name}.v").is_file()]:
        case [rtl_dir]:
            candidates = (rtl_dir / f"{tile.name}{s}.v" for s in _TILE_RTL_SUFFIXES)
            return [path for path in candidates if path.is_file()]
        case []:
            raise FileNotFoundError(
                f"No generated RTL '{tile.name}.v' in "
                f"{', '.join(map(str, search_dirs))}. Run `gen_fabric` first."
            )
        case rtl_dirs:
            raise ValueError(
                f"Ambiguous RTL for tile {tile.name}: {', '.join(map(str, rtl_dirs))}"
            )


def collect_fabric_rtl(fabric: Fabric, project_dir: Path) -> list[Path]:
    """Collect the Verilog sources of the generated fabric from its model.

    Only used tiles are read; `Tile/` may hold unused tiles or variants that
    define the same modules.

    Parameters
    ----------
    fabric : Fabric
        The loaded fabric model.
    project_dir : Path
        The FABulous project directory.

    Returns
    -------
    list[Path]
        Resolved, de-duplicated sources: `Fabric/*.v`, `models_pack`, the
        RTL of every used tile and supertile, and their BEL sources.

    Raises
    ------
    FileNotFoundError
        If `Fabric/` holds no Verilog sources.
    """
    files = sorted((project_dir / "Fabric").glob("*.v"))
    if not files:
        raise FileNotFoundError(
            f"No Verilog sources in {project_dir / 'Fabric'}. Run `gen_fabric` first."
        )

    if (models_pack := get_context().models_pack) is not None:
        files.append(models_pack)

    for tile in chain(fabric.tileDic.values(), fabric.superTileDic.values()):
        files.extend(_tile_rtl(tile))

    # Tiles may ship their own copy of a shared BEL; FABulous treats a BEL
    # module name as one primitive, so one source per module is read.
    bel_sources: dict[str, Path] = {}
    for bel in fabric.getAllUniqueBels():
        bel_sources.setdefault(bel.module_name, bel.src)
    files.extend(bel_sources.values())

    return list(dict.fromkeys(f.resolve() for f in files))


def gen_emulation_netlist(
    rtl_files: list[Path],
    bitstream_vh: Path,
    netlist_path: Path,
    *,
    top_name: str,
    fabric_name: str,
) -> None:
    """Fold the fabric RTL under an emulation bitstream into a flat netlist.

    The Yosys script (`.ys`), log (`.log`) and cell statistics (`.stat.txt`)
    are written next to the netlist.

    Parameters
    ----------
    rtl_files : list[Path]
        Fabric Verilog sources, e.g. from `collect_fabric_rtl`.
    bitstream_vh : Path
        The emulation bitstream defines written by `bit_gen`.
    netlist_path : Path
        Output Verilog netlist; its top module is `top_name`.
    top_name : str
        The module to fold, normally `<fabric name>_top`.
    fabric_name : str
        The fabric module below `top_name` that instantiates the tiles.

    Raises
    ------
    FileNotFoundError
        If `bitstream_vh` does not exist.
    RuntimeError
        If Yosys fails, including when a combinational loop or a latch is
        left after folding.
    """
    if not bitstream_vh.is_file():
        raise FileNotFoundError(f"Emulation bitstream {bitstream_vh} not found.")

    netlist_path.parent.mkdir(parents=True, exist_ok=True)
    script_path = netlist_path.with_suffix(".ys")
    log_path = netlist_path.with_suffix(".log")
    stat_path = netlist_path.with_suffix(".stat.txt")
    script_path.write_text(
        YosysTool.render_template(
            "yosys_emulation_netlist.j2",
            bitstream_vh=bitstream_vh.resolve(),
            rtl_files=rtl_files,
            top_name=top_name,
            fabric_name=fabric_name,
            stat_path=stat_path.resolve(),
            netlist_path=netlist_path.resolve(),
        )
    )

    logger.info(f"Folding {top_name} under {bitstream_vh} ({len(rtl_files)} files)")
    try:
        YosysTool.run(args=["-q", "-l", str(log_path), "-s", str(script_path)])
    except RuntimeError as e:
        raise RuntimeError(f"Emulation netlist failed, see {log_path}. {e}") from e
    logger.info(f"Emulation netlist written to {netlist_path} (stats: {stat_path})")
