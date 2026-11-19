"""Tests for folding a configured fabric into an emulation netlist."""

import re
from pathlib import Path

import pytest

from fabulous.fabric_generator.gen_fabric.gen_emulation_netlist import (
    collect_fabric_rtl,
    gen_emulation_netlist,
)
from fabulous.fabulous_repl.fabulous_repl import FABulousREPL
from fabulous.tools.yosys import YosysTool
from tests.conftest import make_default_project, run_cmd
from tests.fabric_gen_test.integration_test.conftest import compile_user_design

# Smallest demo-tile fabric that fits the counter below.
_SMALL_FABRIC = (
    "FabricBegin\n"
    "NULL,N_term_single,N_term_single,NULL\n"
    "W_IO,LUT4AB,LUT4AB,NULL\n"
    "W_IO,LUT4AB,LUT4AB,NULL\n"
    "NULL,S_term_single,S_term_single,NULL\n"
)
_USED_TILES = {"LUT4AB", "W_IO", "N_term_single", "S_term_single"}

_COUNTER_BITS = 2
_COUNTER = f"""\
module ctr (input wire rst, input wire ena, output wire [{_COUNTER_BITS - 1}:0] d);
    wire clk;
    (* keep *) Global_Clock clk_i (.CLK(clk));
    reg [{_COUNTER_BITS - 1}:0] c;
    always @(posedge clk) if (rst) c <= '0; else if (ena) c <= c + 1'b1;
    assign d = c;
endmodule
"""
_COUNTER_PCF = (
    "set_io rst X0Y1/A\nset_io ena X0Y1/B\nset_io d[0] X0Y2/A\nset_io d[1] X0Y2/B\n"
)

_EQUIV_SEQ_DEPTH = 5


@pytest.fixture
def fabulous_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Demo project shrunk to `_SMALL_FABRIC` before `cli` loads it."""
    project_dir = make_default_project(tmp_path, monkeypatch)
    fabric_csv = project_dir / "fabric.csv"
    text = fabric_csv.read_text()
    begin, end = text.index("FabricBegin"), text.index("FabricEnd")
    fabric_csv.write_text(text[:begin] + _SMALL_FABRIC + text[end:])
    return project_dir


@pytest.fixture
def generated_cli(cli: FABulousREPL) -> FABulousREPL:
    """CLI bound to the small fabric with all fabric RTL generated."""
    run_cmd(cli, "run_fab")
    assert cli.exit_code == 0
    return cli


@pytest.fixture
def counter_vh(generated_cli: FABulousREPL, fabulous_project: Path) -> Path:
    """Emulation bitstream of the counter placed on the small fabric."""
    design = fabulous_project / "user_design" / "ctr.v"
    design.write_text(_COUNTER)
    pcf = design.with_suffix(".pcf")
    pcf.write_text(_COUNTER_PCF)
    return compile_user_design(generated_cli, design, "ctr", pcf).with_suffix(".vh")


def test_collect_fabric_rtl_reads_only_used_tiles(
    generated_cli: FABulousREPL, fabulous_project: Path
) -> None:
    """Unused tiles in `Tile/` are not read."""
    tile_root = fabulous_project / "Tile"
    files = collect_fabric_rtl(generated_cli.fabulousAPI.fabric, fabulous_project)

    tile_dirs = {
        f.relative_to(tile_root).parts[0] for f in files if tile_root in f.parents
    }
    assert tile_dirs == _USED_TILES
    assert len(files) == len(set(files))
    assert (fabulous_project / "Fabric" / "eFPGA.v").resolve() in files


def test_collect_fabric_rtl_requires_generated_tiles(
    cli: FABulousREPL, fabulous_project: Path
) -> None:
    """Collecting before `gen_fabric` fails."""
    with pytest.raises(FileNotFoundError, match="gen_fabric"):
        collect_fabric_rtl(cli.fabulousAPI.fabric, fabulous_project)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("wire w; assign w = a ^ w; assign y = w;", "SCC"),
        ("reg q; always @* if (a) q = b; assign y = q;", "Assertion failed"),
    ],
    ids=["combinational_loop", "latch"],
)
def test_gen_emulation_netlist_rejects_unclean_netlist(
    tmp_path: Path, body: str, reason: str
) -> None:
    """A loop or latch left after folding is an error."""
    rtl = tmp_path / "bad.v"
    ports = "(input a, input b, output y);"
    rtl.write_text(
        f"module bad_tile{ports} {body} endmodule\n"
        f"module bad_fabric{ports} bad_tile t(a, b, y); endmodule\n"
        f"module bad_top{ports} bad_fabric f(a, b, y); endmodule\n"
    )
    vh = tmp_path / "empty.vh"
    vh.write_text("")

    with pytest.raises(RuntimeError, match=reason):
        gen_emulation_netlist(
            [rtl], vh, tmp_path / "out.v", top_name="bad_top", fabric_name="bad_fabric"
        )


def test_gen_emulation_netlist_requires_bitstream(tmp_path: Path) -> None:
    """A missing `.vh` is reported before Yosys runs."""
    with pytest.raises(FileNotFoundError, match="missing.vh"):
        gen_emulation_netlist(
            [],
            tmp_path / "missing.vh",
            tmp_path / "out.v",
            top_name="top",
            fabric_name="fabric",
        )


@pytest.mark.slow
def test_gen_emulation_netlist_is_equivalent_to_rtl(
    generated_cli: FABulousREPL, fabulous_project: Path, counter_vh: Path
) -> None:
    """Only the counter flops remain, and the netlist matches the RTL."""
    run_cmd(generated_cli, f"gen_emulation_netlist {counter_vh}")
    assert generated_cli.exit_code == 0

    netlist = counter_vh.with_name("ctr_emulation.v")
    stat = netlist.with_suffix(".stat.txt").read_text()
    assert "dlatch" not in stat
    dff_count = re.search(r"(\d+)\s+\$dff\b", stat)
    assert dff_count is not None
    assert int(dff_count.group(1)) == _COUNTER_BITS

    rtl_files = collect_fabric_rtl(generated_cli.fabulousAPI.fabric, fabulous_project)
    sources = " ".join(f'"{f}"' for f in [counter_vh, *rtl_files])
    script = netlist.with_name("equiv.ys")
    script.write_text(
        f"read_verilog -sv -DEMULATION {sources}\n"
        "hierarchy -check -top eFPGA_top\n"
        "proc\nflatten\nopt_clean\nrename eFPGA_top gold\ndesign -stash gold\n"
        f'read_verilog "{netlist}"\n'
        "proc\nrename eFPGA_top gate\ndesign -stash gate\n"
        "design -copy-from gold -as gold gold\n"
        "design -copy-from gate -as gate gate\n"
        "equiv_make gold gate equiv\nhierarchy -top equiv\n"
        f"equiv_simple -seq {_EQUIV_SEQ_DEPTH}\n"
        f"equiv_induct -seq {_EQUIV_SEQ_DEPTH}\n"
        "equiv_status -assert\n"
    )
    YosysTool.run(args=["-q", "-s", str(script)])
