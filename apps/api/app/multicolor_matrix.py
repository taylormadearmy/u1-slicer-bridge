"""Multicolor slicing experiment matrix.

Runs Orca against multiple 3MF preparation strategies and settings variants,
then reports whether generated G-code actually uses multiple tools (T1+).

Usage inside API container:
  python /app/multicolor_matrix.py --source /data/uploads/<file>.3mf

Optional:
  --out /cache/slicing/matrix
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from config import get_printer_profile
from profile_embedder import ProfileEmbedder
from slicer import OrcaSlicer


REMOVE_COMMON = {
    "Metadata/project_settings.config",
    "Metadata/slice_info.config",
    "Metadata/cut_information.xml",
}


def load_base_config() -> Dict[str, Any]:
    embedder = ProfileEmbedder(Path("/app/orca_profiles"))
    profiles = embedder.load_snapmaker_profiles()
    cfg = {
        **profiles.printer,
        **profiles.process,
        **profiles.filament,
    }
    cfg.setdefault("layer_gcode", "G92 E0")
    cfg.setdefault("enable_arc_fitting", "1")
    return cfg


def build_filament_settings(variant: str) -> Dict[str, Any]:
    nozzle = ["235", "210", "210", "210"]
    bed = ["70", "60", "60", "60"]
    colors = ["#FF0000", "#00FF00", "#FFFFFF", "#FFFFFF"]

    settings: Dict[str, Any] = {
        "nozzle_temperature": nozzle,
        "nozzle_temperature_initial_layer": nozzle,
        "bed_temperature": bed,
        "bed_temperature_initial_layer": bed,
        "bed_temperature_initial_layer_single": bed[0],
        "cool_plate_temp": bed,
        "cool_plate_temp_initial_layer": bed,
        "textured_plate_temp": bed,
        "textured_plate_temp_initial_layer": bed,
    }

    if variant in {"colors", "colors_types"}:
        settings["extruder_colour"] = colors
        settings["filament_colour"] = colors

    if variant == "colors_types":
        settings["filament_type"] = ["PLA", "PLA", "PLA", "PLA"]
        settings["default_filament_profile"] = [
            "PLA Red",
            "PLA Green",
            "PLA White",
            "PLA White",
        ]

    return settings


def write_3mf_with_project_settings(
    source_3mf: Path,
    output_3mf: Path,
    project_settings: Dict[str, Any],
    remove_metadata: set[str],
) -> None:
    output_3mf.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source_3mf, "r") as src:
        with zipfile.ZipFile(output_3mf, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                if item.filename in remove_metadata:
                    continue
                if item.filename == "Metadata/project_settings.config":
                    continue
                data = src.read(item.filename)
                dst.writestr(item, data)

            dst.writestr("Metadata/project_settings.config", json.dumps(project_settings, indent=2))


def parse_tools(gcode_path: Path) -> Tuple[List[str], int]:
    tool_re = re.compile(r"^T\d+$")
    tools: List[str] = []
    count = 0
    with gcode_path.open("r", errors="ignore") as f:
        for line in f:
            t = line.strip()
            if tool_re.match(t):
                tools.append(t)
                count += 1
    return sorted(set(tools)), count


def run_variant(
    slicer: OrcaSlicer,
    source_3mf: Path,
    out_dir: Path,
    prep_mode: str,
    settings_mode: str,
) -> Dict[str, Any]:
    run_name = f"{prep_mode}__{settings_mode}"
    run_dir = out_dir / run_name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    target_3mf = run_dir / "sliceable.3mf"
    base_cfg = load_base_config()
    base_cfg.update(build_filament_settings(settings_mode))

    remove_set = set(REMOVE_COMMON)
    if prep_mode in {"orig_strip", "rebuild_strip", "orig_keep_model_strip_seq"}:
        remove_set.add("Metadata/filament_sequence.json")

    if prep_mode == "orig_keep_all":
        remove_set = {"Metadata/project_settings.config"}

    if prep_mode in {"orig_strip", "orig_keep_all", "orig_keep_model_strip_seq"}:
        write_3mf_with_project_settings(source_3mf, target_3mf, base_cfg, remove_set)
    elif prep_mode == "rebuild_strip":
        embedder = ProfileEmbedder(Path("/app/orca_profiles"))
        temp_clean = run_dir / "clean.3mf"
        embedder._rebuild_with_trimesh(source_3mf, temp_clean)
        write_3mf_with_project_settings(temp_clean, target_3mf, base_cfg, remove_set)
    else:
        raise ValueError(f"Unknown prep_mode: {prep_mode}")

    result = slicer.slice_3mf(target_3mf, run_dir)
    summary: Dict[str, Any] = {
        "run": run_name,
        "prep_mode": prep_mode,
        "settings_mode": settings_mode,
        "success": result["success"],
        "exit_code": result["exit_code"],
        "stderr_head": (result["stderr"] or "")[:300],
        "tools": [],
        "tool_change_count": 0,
        "multicolor_tools": False,
    }

    if result["success"]:
        gcode_files = sorted(run_dir.glob("plate_*.gcode"))
        if gcode_files:
            tools, count = parse_tools(gcode_files[0])
            summary["tools"] = tools
            summary["tool_change_count"] = count
            summary["multicolor_tools"] = any(t != "T0" for t in tools)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Path to source 3MF")
    parser.add_argument("--out", default="/cache/slicing/multicolor-matrix", help="Output directory")
    args = parser.parse_args()

    source = Path(args.source)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    slicer = OrcaSlicer(get_printer_profile("snapmaker_u1"))

    prep_modes = [
        "orig_strip",
        "orig_keep_all",
        "rebuild_strip",
    ]
    settings_modes = [
        "minimal",
        "colors",
        "colors_types",
    ]

    results: List[Dict[str, Any]] = []
    for prep in prep_modes:
        for settings in settings_modes:
            try:
                summary = run_variant(slicer, source, out_dir, prep, settings)
            except Exception as e:
                summary = {
                    "run": f"{prep}__{settings}",
                    "prep_mode": prep,
                    "settings_mode": settings,
                    "success": False,
                    "exit_code": -1,
                    "stderr_head": str(e),
                    "tools": [],
                    "tool_change_count": 0,
                    "multicolor_tools": False,
                }
            results.append(summary)

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
