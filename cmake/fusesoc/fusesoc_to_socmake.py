import argparse
import sys
from typing import Any, Dict, List
import yaml
from pathlib import Path

import re


class FusesocToSocmakeError(Exception):
    """Raised for malformed FuseSoC .core input that this converter can't handle."""


def removesuffix(s: str, suffix: str) -> str:
    """Backport of str.removesuffix for Python < 3.9"""
    if suffix and s.endswith(suffix):
        return s[: -len(suffix)]
    return s


def convert_language(lang: str) -> str:
    """Convert a FuseSoC file type string to a SoCMake language name.

    Strips the trailing ``Source`` suffix and uppercases the result.
    For example, ``systemVerilogSource`` becomes ``SYSTEMVERILOG``.

    Args:
        lang: FuseSoC file type string (e.g. ``systemVerilogSource``).

    Returns:
        SoCMake language name (e.g. ``SYSTEMVERILOG``).
    """
    lang = removesuffix(lang, "Source")
    lang = lang.upper()
    return lang


def convert_fusesoc_vlnv_to_socmake_add_ip_args(vlnv: str) -> str:
    """Convert a FuseSoC VLNV string to SoCMake ``add_ip()`` named argument string.

    Args:
        vlnv: FuseSoC VLNV string in ``vendor:library:name:version`` form.

    Returns:
        String of named CMake keyword arguments (e.g. ``name VENDOR v LIBRARY l VERSION ver``).
    """
    parts: list[str] = vlnv.split(":")

    vendor: str = f"VENDOR {parts[0]}" if parts[0] else ""
    lib: str = f"LIBRARY {parts[1]}" if parts[1] else ""
    name: str = f"{parts[2]}"
    if len(parts) == 4:
        version: str = f"VERSION {parts[3]}" if parts[3] else ""
    else:
        version = ""

    return f"{name} {vendor} {lib} {version}"


def convert_fusesoc_vlnv_to_socmake_ip(vlnv: str) -> str:
    """Convert a FuseSoC VLNV string to a SoCMake IP reference argument string.

    Args:
        vlnv: FuseSoC VLNV string in ``vendor:library:name:version`` form.

    Returns:
        String of named CMake keyword arguments (e.g. ``name VENDOR v LIBRARY l VERSION ver``).
    """
    parts: list[str] = vlnv.split(":")

    vendor: str = f"VENDOR {parts[0]}" if parts[0] else ""
    lib: str = f"LIBRARY {parts[1]}" if parts[1] else ""
    name: str = f"{parts[2]}"
    if len(parts) == 4:
        version: str = f"VERSION {parts[3]}" if parts[3] else ""
    else:
        version = ""

    return f"{name} {vendor} {lib} {version}"


def move_prefix_to_end(s: str) -> str:
    """Strip a version constraint operator prefix from a VLNV string.

    Recognizes operators ``>=``, ``<=``, ``>``, ``<``, ``~``, ``^`` at the
    start of the string and removes them.

    Args:
        s: VLNV string optionally prefixed with a version constraint operator.

    Returns:
        The VLNV string with the leading operator removed.
    """
    # Match any of the operators at the start
    match = re.match(r"^(>=|<=|>|<|~|\^)(.*)$", s)
    if match:
        op, rest = match.groups()
        # return rest + op
        return rest
    return s


def vlnv_to_file_set_name(vlnv: str) -> str:
    """Convert a VLNV string to a default SoCMake ``FILE_SET`` name.

    Uppercases the VLNV and replaces every ``:`` with ``_``, e.g.
    ``lowrisc:prim_generic:and2`` becomes ``LOWRISC_PRIM_GENERIC_AND2``.

    Args:
        vlnv: FuseSoC VLNV string.

    Returns:
        A SoCMake ``FILE_SET`` name derived from the VLNV.
    """
    return vlnv.upper().replace(":", "_")


def convert_depend_vlnv(vlnv: str) -> str:
    """Convert a FuseSoC dependency VLNV string to SoCMake ``::``-separated format.

    Strips any leading version constraint operator and joins non-empty VLNV
    components with ``::``.

    Args:
        vlnv: FuseSoC dependency VLNV string (e.g. ``>=vendor:library:name:1.0``).

    Returns:
        SoCMake-style VLNV string (e.g. ``vendor::library::name::1.0``).
    """
    dep: str = move_prefix_to_end(vlnv)
    dep = "::".join([x for x in dep.split(":") if x])
    return dep


def append_and_create(dict_ref: Dict[Any, List[Any]], key: Any, val: Any) -> None:
    """Append a value to a list in a dictionary, creating the list if the key is absent.

    Does nothing if ``val`` is already present in ``dict_ref[key]``.

    Args:
        dict_ref: The dictionary to update in place.
        key: The key under which to append ``val``.
        val: The value to append.
    """
    if key not in dict_ref:
        dict_ref[key] = [val]
    else:
        if val not in dict_ref[key]:
            dict_ref[key].append(val)


def fusesoc_to_socmake(input_file: Path, file_set: str | None = None):
    """Parse a FuseSoC ``.core`` YAML file and print equivalent SoCMake CMake commands to stdout.

    Reads the FuseSoC core description and emits the corresponding
    ``add_ip``, ``ip_sources``, ``ip_include_directories``, and ``ip_link``
    CMake calls.

    If the core declares one or more ``virtual`` VLNs (per the CAPI2 spec,
    ``virtual`` is always a list, though a bare version-less VLN, not a full
    VLNV), one IP is created per virtual VLN instead of a single IP named
    after the core's own ``name``. Every ``FILE_SET`` generated for this core
    is renamed to ``file_set`` (if given) or, otherwise, to the default
    derived from the core's own ``name`` (see :func:`vlnv_to_file_set_name`).

    This split exists because a ``virtual`` VLN is a technology-agnostic
    handle that several core files may all provide (e.g. ``prim_generic`` and
    ``prim_xilinx`` implementations of the same primitive cell): FuseSoC
    itself resolves such VLNs to exactly one concrete provider per build, but
    add_ip_from_fusesoc doesn't perform that resolution, so it instead lets
    every providing core attach its sources to the *same shared* ``add_ip()``
    target (CMake ``ALIAS`` targets can't be repointed, so this is the only
    way for a ``depend: - <virtual vln>`` elsewhere to resolve regardless of
    which providers were pulled in) and relies on ``FILE_SET`` -- keyed by
    each provider's own ``name``, since the fileset names internal to a core
    only ever separate by language there, never by competing content -- to
    keep the providers' sources distinguishable so a downstream
    ``generate_sv_sources_list(FILE_SETS ...)`` can select exactly one later.
    A non-virtual core has no such ambiguity, so it keeps its own fileset
    names as ``FILE_SET`` unchanged.

    Args:
        input_file: Path to the FuseSoC ``.core`` YAML file.
        file_set: Manual override for the ``FILE_SET`` name used when the
            core declares ``virtual`` VLNs. Ignored otherwise.
    """
    with open(input_file, "r") as f:
        core_data = yaml.safe_load(f)

    real_ip_vlnv: str = core_data.get("name")

    virtual: str | List[str] | None = core_data.get("virtual", None)
    file_set_override: str | None = None
    if virtual:
        # Per the CAPI2 schema 'virtual' is always a list, but tolerate a
        # bare string too in case a core file was hand-written non-strictly.
        ip_vlnv_list: List[str] = virtual if isinstance(virtual, list) else [virtual]
        file_set_override = (
            file_set if file_set else vlnv_to_file_set_name(real_ip_vlnv)
        )
    else:
        ip_vlnv_list = [real_ip_vlnv]

    ip_description: str | None = core_data.get("description", None)
    if ip_description:
        ip_description = ip_description.replace(";", "")
    description_arg: str = ""
    if ip_description:
        description_arg = f'DESCRIPTION "{ip_description}"'

    dependencies: list[str] = []
    # Dictionary of (language, fileset, headers) -> list[files...]
    files_list: dict[tuple[str, str, bool], list[str]] = {}
    # Dictionary of (language, fileset) -> list[dirs...]
    incdirs: dict[tuple[str, str], list[str]] = {}

    # Handle filesets
    filesets = core_data.get("filesets", {})
    for fusesoc_file_set_name, fs_data in filesets.items():
        # A virtual core attaches its sources to the shared file set name
        # instead of its own fileset name (see file_set_override above).
        # fusesoc_file_set_name is kept around (unmodified) purely so error
        # messages below can point at the fileset as written in the .core
        # file, regardless of which name its sources end up filed under.
        file_set_name = file_set_override if file_set_override else fusesoc_file_set_name
        files = fs_data.get("files", [])
        file_set_file_type: str | None = fs_data.get("file_type", None)

        depend: list[str] | None = fs_data.get("depend", None)
        if depend:
            for dep in depend:
                dep_vlnv: str = convert_depend_vlnv(dep)
                if dep_vlnv not in dependencies:
                    dependencies.append(dep_vlnv)

        if files:
            for f in files:
                if isinstance(f, dict):
                    file_path = list(f.keys())[0]
                    is_include_file: bool = f[file_path].get("is_include_file", False)
                    file_type: str | None = f[file_path].get(
                        "file_type", file_set_file_type
                    )
                    if not file_type:
                        raise FusesocToSocmakeError(
                            f"{input_file}: file '{file_path}' in fileset "
                            f"'{fusesoc_file_set_name}' has no 'file_type', and "
                            "the fileset has no default 'file_type' either. "
                            "Set one of the two."
                        )
                    incpath: str | None = f[file_path].get("include_path", None)
                    append_and_create(
                        files_list,
                        (convert_language(file_type), file_set_name, is_include_file),
                        file_path,
                    )
                    if incpath:
                        append_and_create(
                            incdirs,
                            (convert_language(file_type), file_set_name),
                            incpath,
                        )
                else:
                    if not file_set_file_type:
                        raise FusesocToSocmakeError(
                            f"{input_file}: fileset '{fusesoc_file_set_name}' lists "
                            f"file '{f}' as a plain string but has no 'file_type'. "
                            "Either set the fileset's 'file_type', or give the file "
                            "its own by listing it as "
                            f"'{f}: {{file_type: ...}}' instead."
                        )
                    append_and_create(
                        files_list,
                        (convert_language(file_set_file_type), file_set_name, False),
                        f,
                    )

    # Everything above (files_list, incdirs, dependencies) is derived purely
    # from the core file's own filesets, independently of which VLN(s) the
    # resulting IP(s) are registered under. Emit one full add_ip()/
    # ip_sources()/ip_include_directories()/ip_link() block per VLN in
    # ip_vlnv_list (normally just the core's own name; one block per
    # 'virtual' entry when present). add_ip() itself is idempotent -- if
    # another core already registered the same VLN it reuses that target --
    # so several providers of the same virtual VLN safely merge into one IP,
    # each contributing its own FILE_SET.
    for ip_vlnv in ip_vlnv_list:
        add_ip_vlnv_args: str = convert_fusesoc_vlnv_to_socmake_add_ip_args(ip_vlnv)
        # print(f'add_ip({add_ip_vlnv_args} {description_arg} NO_ALIAS)\n')
        print(f"add_ip({add_ip_vlnv_args} {description_arg})\n")

        for file_attributes, files in files_list.items():
            print(
                f"ip_sources(${{IP}} {file_attributes[0]} FILE_SET {file_attributes[1]} {'HEADERS' if file_attributes[2] else ''}"
            )  # )
            for file in files:
                print(f"    ${{CMAKE_CURRENT_LIST_DIR}}/{file}")
            print(")\n")

        for file_attributes, dirs in incdirs.items():
            print(
                f"ip_include_directories(${{IP}} {file_attributes[0]} FILE_SET {file_attributes[1]}"
            )  # )
            for dir in dirs:
                print(f"    ${{CMAKE_CURRENT_LIST_DIR}}/{dir}")
            print(")\n")

        if dependencies:
            print("ip_link(${IP}")  # )
            for dep in dependencies:
                print(f"    {dep}")
            print(")\n")


def main():
    """Entry point: parse command-line arguments and run the FuseSoC-to-SoCMake conversion."""
    parser = argparse.ArgumentParser(
        description="Convert FuseSoC .core (YAML) files to SoCMake CMakeLists.txt"
    )
    parser.add_argument("input", type=Path, help="Path to FuseSoC .core YAML file")
    parser.add_argument(
        "--file-set",
        type=str,
        default=None,
        help=(
            "Override the FILE_SET name used when the core declares a "
            "'virtual' VLNV. Ignored if the core has no 'virtual' entry. "
            "Defaults to the core's own name, uppercased with ':' replaced "
            "by '_' (e.g. lowrisc:prim_generic:and2 -> "
            "LOWRISC_PRIM_GENERIC_AND2)."
        ),
    )

    args = parser.parse_args()
    try:
        fusesoc_to_socmake(args.input, file_set=args.file_set)
    except FusesocToSocmakeError as e:
        # Printed to stderr, not stdout: stdout only ever carries generated
        # CMake code (see add_ip_from_fusesoc.cmake, which captures it via
        # OUTPUT_VARIABLE and treats a non-zero exit as fatal).
        print(f"fusesoc_to_socmake: error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
