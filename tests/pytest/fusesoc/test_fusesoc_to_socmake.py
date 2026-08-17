"""Tests for cmake/fusesoc/fusesoc_to_socmake.py.

These test the pure Python text-generation and error-handling logic
directly, without going through CMake. See tests/ctests/fusesoc/ for the
CMakeTest-based integration tests that exercise the full
add_ip_from_fusesoc() pipeline (real add_ip()/ip_sources() calls, target
merging across multiple .core files, the FATAL_ERROR path, caching).
"""

import subprocess
import sys
from pathlib import Path

import pytest

from fusesoc_to_socmake import (
    FusesocToSocmakeError,
    convert_depend_vlnv,
    convert_fusesoc_vlnv_to_socmake_add_ip_args,
    convert_language,
    fusesoc_to_socmake,
    move_prefix_to_end,
    vlnv_to_file_set_name,
)

FUSESOC_TO_SOCMAKE_PY = Path(__file__).resolve().parents[3] / "cmake" / "fusesoc" / "fusesoc_to_socmake.py"


def write_core(tmp_path: Path, content: str, name: str = "test.core") -> Path:
    """Write a FuseSoC .core file (with the conventional CAPI=2: header) to tmp_path."""
    core_file = tmp_path / name
    core_file.write_text("CAPI=2:\n" + content)
    return core_file


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lang,expected",
    [
        ("systemVerilogSource", "SYSTEMVERILOG"),
        ("verilogSource", "VERILOG"),
        ("vhdlSource", "VHDL"),
        ("vlt", "VLT"),
        ("waiver", "WAIVER"),
    ],
)
def test_convert_language(lang, expected):
    assert convert_language(lang) == expected


@pytest.mark.parametrize(
    "prefixed,expected",
    [
        (">=acme:lib:name:1.0", "acme:lib:name:1.0"),
        ("<=acme:lib:name:1.0", "acme:lib:name:1.0"),
        (">acme:lib:name", "acme:lib:name"),
        ("<acme:lib:name", "acme:lib:name"),
        ("~acme:lib:name", "acme:lib:name"),
        ("^acme:lib:name", "acme:lib:name"),
        ("acme:lib:name", "acme:lib:name"),
    ],
)
def test_move_prefix_to_end(prefixed, expected):
    assert move_prefix_to_end(prefixed) == expected


@pytest.mark.parametrize(
    "vlnv,expected",
    [
        (">=acme:lib:name:1.0", "acme::lib::name::1.0"),
        ("acme:lib:name", "acme::lib::name"),
        ("acme::lib::name", "acme::lib::name"),
    ],
)
def test_convert_depend_vlnv(vlnv, expected):
    assert convert_depend_vlnv(vlnv) == expected


def test_vlnv_to_file_set_name():
    assert vlnv_to_file_set_name("lowrisc:prim_generic:and2") == "LOWRISC_PRIM_GENERIC_AND2"


def test_convert_fusesoc_vlnv_to_socmake_add_ip_args_full():
    args = convert_fusesoc_vlnv_to_socmake_add_ip_args("acme:lib:name:1.2.3")
    assert args == "name VENDOR acme LIBRARY lib VERSION 1.2.3"


def test_convert_fusesoc_vlnv_to_socmake_add_ip_args_no_version():
    args = convert_fusesoc_vlnv_to_socmake_add_ip_args("acme:lib:name")
    assert args == "name VENDOR acme LIBRARY lib "


# ---------------------------------------------------------------------------
# fusesoc_to_socmake(): non-virtual cores
# ---------------------------------------------------------------------------


def test_non_virtual_basic(tmp_path, capsys):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo:1.0"
description: "A foo IP"
filesets:
  files_rtl:
    files:
      - rtl/foo.sv
    file_type: systemVerilogSource
targets:
  default:
    filesets: [files_rtl]
""",
    )
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    assert "add_ip(foo VENDOR acme LIBRARY lib VERSION 1.0" in out
    assert 'DESCRIPTION "A foo IP"' in out
    assert "ip_sources(${IP} SYSTEMVERILOG FILE_SET files_rtl" in out
    assert "${CMAKE_CURRENT_LIST_DIR}/rtl/foo.sv" in out


def test_non_virtual_keeps_filesets_distinct_by_own_name(tmp_path, capsys):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    files: [rtl/foo.sv]
    file_type: systemVerilogSource
  files_verilator_waiver:
    files: [lint/foo.vlt]
    file_type: vlt
targets:
  default:
    filesets: [files_rtl, files_verilator_waiver]
""",
    )
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    # Each fileset keeps its own fusesoc name as FILE_SET -- not merged,
    # since there's no 'virtual' entry forcing a shared identity.
    assert "ip_sources(${IP} SYSTEMVERILOG FILE_SET files_rtl" in out
    assert "ip_sources(${IP} VLT FILE_SET files_verilator_waiver" in out


def test_depend_only_fileset_needs_no_file_type(tmp_path, capsys):
    """A fileset with only 'depend:' (no files) must not require a file_type."""
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    files: [rtl/foo.sv]
    file_type: systemVerilogSource
  files_lint_waiver:
    depend:
      - acme:lint:common
targets:
  default:
    filesets: [files_rtl, files_lint_waiver]
""",
    )
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    assert "ip_link(${IP}" in out
    assert "acme::lint::common" in out
    # No ip_sources() block was emitted for the depend-only fileset.
    assert "files_lint_waiver" not in out


def test_depend_deduplicated_across_filesets(tmp_path, capsys):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_a:
    depend: [acme:lint:common]
  files_b:
    depend: [acme:lint:common]
targets:
  default:
    filesets: [files_a, files_b]
""",
    )
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    assert out.count("acme::lint::common") == 1


def test_include_path_generates_include_directories(tmp_path, capsys):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    file_type: systemVerilogSource
    files:
      - rtl/foo.sv:
          include_path: rtl/include
targets:
  default:
    filesets: [files_rtl]
""",
    )
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    assert "ip_include_directories(${IP} SYSTEMVERILOG FILE_SET files_rtl" in out
    assert "${CMAKE_CURRENT_LIST_DIR}/rtl/include" in out


def test_is_include_file_generates_headers(tmp_path, capsys):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    file_type: systemVerilogSource
    files:
      - rtl/foo_pkg.svh:
          is_include_file: true
targets:
  default:
    filesets: [files_rtl]
""",
    )
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    assert "HEADERS" in out


def test_per_file_type_override(tmp_path, capsys):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    file_type: systemVerilogSource
    files:
      - rtl/foo.sv
      - lint/foo.vlt:
          file_type: vlt
targets:
  default:
    filesets: [files_rtl]
""",
    )
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    assert "ip_sources(${IP} SYSTEMVERILOG FILE_SET files_rtl" in out
    assert "ip_sources(${IP} VLT FILE_SET files_rtl" in out


# ---------------------------------------------------------------------------
# fusesoc_to_socmake(): virtual cores
# ---------------------------------------------------------------------------


AND2_CORE = """
name: "lowrisc:prim_generic:and2"
description: "Generic 2-input and"
virtual:
  - lowrisc:prim:and2
filesets:
  files_rtl:
    files: [rtl/prim_and2.sv]
    file_type: systemVerilogSource
targets:
  default:
    filesets: [files_rtl]
"""


def test_virtual_single_default_file_set(tmp_path, capsys):
    core = write_core(tmp_path, AND2_CORE)
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    # add_ip() is registered under the virtual VLN, not the core's own name.
    assert "add_ip(and2 VENDOR lowrisc LIBRARY prim" in out
    assert "prim_generic" not in out.split("\n")[0]
    # FILE_SET is derived from the core's own (real) name.
    assert "FILE_SET LOWRISC_PRIM_GENERIC_AND2" in out


def test_virtual_file_set_override(tmp_path, capsys):
    core = write_core(tmp_path, AND2_CORE)
    fusesoc_to_socmake(core, file_set="MY_CUSTOM_SET")
    out = capsys.readouterr().out

    assert "FILE_SET MY_CUSTOM_SET" in out
    assert "LOWRISC_PRIM_GENERIC_AND2" not in out


def test_virtual_multiple_entries(tmp_path, capsys):
    core = write_core(
        tmp_path,
        """
name: "acme:prim_dual:andor2"
description: "Combined AND/OR primitive"
virtual:
  - acme:prim:and2
  - acme:prim:or2
filesets:
  files_rtl:
    files: [rtl/prim_andor2.sv]
    file_type: systemVerilogSource
targets:
  default:
    filesets: [files_rtl]
""",
    )
    fusesoc_to_socmake(core)
    out = capsys.readouterr().out

    assert "add_ip(and2 VENDOR acme LIBRARY prim" in out
    assert "add_ip(or2 VENDOR acme LIBRARY prim" in out
    # Both providers share the same FILE_SET, derived from the one real name.
    assert out.count("FILE_SET ACME_PRIM_DUAL_ANDOR2") == 2


# ---------------------------------------------------------------------------
# Error handling: missing file_type
# ---------------------------------------------------------------------------


def test_missing_file_type_plain_string_raises(tmp_path):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    files:
      - rtl/foo.sv
targets:
  default:
    filesets: [files_rtl]
""",
    )
    with pytest.raises(FusesocToSocmakeError, match="file_type"):
        fusesoc_to_socmake(core)


def test_missing_file_type_dict_style_raises(tmp_path):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    files:
      - rtl/foo.sv:
          is_include_file: true
targets:
  default:
    filesets: [files_rtl]
""",
    )
    with pytest.raises(FusesocToSocmakeError, match="file_type"):
        fusesoc_to_socmake(core)


def test_missing_file_type_error_names_fileset_and_file(tmp_path):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    files:
      - rtl/foo.sv
targets:
  default:
    filesets: [files_rtl]
""",
    )
    with pytest.raises(FusesocToSocmakeError) as exc_info:
        fusesoc_to_socmake(core)
    assert "files_rtl" in str(exc_info.value)
    assert "rtl/foo.sv" in str(exc_info.value)


# ---------------------------------------------------------------------------
# main() / CLI, exercised as a real subprocess
# ---------------------------------------------------------------------------


def test_cli_success(tmp_path):
    core = write_core(tmp_path, AND2_CORE)
    result = subprocess.run(
        [sys.executable, str(FUSESOC_TO_SOCMAKE_PY), str(core)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "add_ip(and2 VENDOR lowrisc LIBRARY prim" in result.stdout
    assert result.stderr == ""


def test_cli_failure_has_no_traceback(tmp_path):
    core = write_core(
        tmp_path,
        """
name: "acme:lib:foo"
filesets:
  files_rtl:
    files:
      - rtl/foo.sv
targets:
  default:
    filesets: [files_rtl]
""",
    )
    result = subprocess.run(
        [sys.executable, str(FUSESOC_TO_SOCMAKE_PY), str(core)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert "fusesoc_to_socmake: error:" in result.stderr
    assert "file_type" in result.stderr


def test_cli_file_set_flag(tmp_path):
    core = write_core(tmp_path, AND2_CORE)
    result = subprocess.run(
        [sys.executable, str(FUSESOC_TO_SOCMAKE_PY), str(core), "--file-set", "MY_CUSTOM_SET"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "FILE_SET MY_CUSTOM_SET" in result.stdout
