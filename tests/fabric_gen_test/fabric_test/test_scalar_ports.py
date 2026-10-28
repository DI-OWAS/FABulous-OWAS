"""One-wire routing ports spanning one tile are generated as scalars."""

import re
from pathlib import Path

from fabulous.fabulous_repl.fabulous_repl import FABulousREPL
from tests.conftest import run_cmd


def test_one_bit_wire_ports_are_scalar(
    cli: FABulousREPL, fabulous_project: Path
) -> None:
    """The LUT4AB carry (`NORTH,Co,0,-1,Ci,1`) has no `[0:0]` range anywhere."""
    run_cmd(cli, "gen_all_tile")
    run_cmd(cli, "gen_fabric")

    tile = (fabulous_project / "Tile" / "LUT4AB" / "LUT4AB.v").read_text()
    assert re.search(r"^\s*input\s+Ci,", tile, re.MULTILINE)
    assert re.search(r"^\s*output\s+Co,", tile, re.MULTILINE)
    assert ".Ci0(Ci)" in tile

    for path in (fabulous_project / "Tile").rglob("*.v"):
        assert not re.search(r"(input|output)\s+\[0:0\]", path.read_text()), path

    fabric = (fabulous_project / "Fabric" / "eFPGA.v").read_text()
    assert re.search(r"^wire Tile_X\d+Y\d+_Co;", fabric, re.MULTILINE)
    assert not re.search(r"wire\s*\[0:0\]\s*Tile_X\d+Y\d+_Co\b", fabric)
