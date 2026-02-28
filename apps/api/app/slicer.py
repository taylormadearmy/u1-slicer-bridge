"""Orca Slicer orchestration for G-code generation."""

import asyncio
import json
import os
import shutil
import subprocess
import threading
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Callable
from dataclasses import dataclass

from config import PrinterProfile
from gcode_parser import parse_orca_metadata

# Maximum concurrent OrcaSlicer processes (memory-bound).
# Configurable via env var, default 2.
MAX_CONCURRENT_SLICES = int(os.environ.get("MAX_CONCURRENT_SLICES", "2"))
_slicer_semaphore = None

# Active slicer subprocesses keyed by job_id for cancellation support.
_active_processes: Dict[str, subprocess.Popen] = {}


def _get_slicer_semaphore():
    """Lazy-init semaphore (must be created within a running event loop)."""
    global _slicer_semaphore
    if _slicer_semaphore is None:
        _slicer_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SLICES)
    return _slicer_semaphore


def cancel_slice_job(job_id: str) -> bool:
    """Kill the OrcaSlicer process for *job_id* if it is still running.

    Returns True if a process was found and killed, False otherwise.
    """
    proc = _active_processes.pop(job_id, None)
    if proc is None:
        return False
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
    return True


@dataclass
class FilamentData:
    material: str
    nozzle_temp: int
    bed_temp: int
    print_speed: int


@dataclass
class ObjectData:
    id: int
    name: str
    normalized_path: str


class SlicingError(Exception):
    """Raised when slicing fails."""
    pass


class SlicingCancelledError(SlicingError):
    """Raised when slicing is cancelled by the user."""
    pass


class OrcaSlicer:
    """Orchestrates Orca Slicer CLI for headless G-code generation."""

    def __init__(self, printer_profile: PrinterProfile):
        self.printer_profile = printer_profile
        self.orca_bin = Path("/usr/local/bin/orca-slicer")
        self.base_profile_path = Path("/app/orca_profiles/base_snapmaker_u1.json")
        self.filament_template_path = Path("/app/orca_profiles/filament_template.json")

    def generate_profile(
        self,
        filament: FilamentData,
        layer_height: float = 0.2,
        infill_density: int = 15,
        supports: bool = False
    ) -> Dict:
        """Generate Orca JSON profile from base + filament settings."""
        # Load base profile
        with open(self.base_profile_path, 'r') as f:
            profile = json.load(f)

        # Load filament template
        with open(self.filament_template_path, 'r') as f:
            filament_config = json.load(f)

        # Apply filament values
        filament_config['filament_type'] = filament.material
        filament_config['nozzle_temperature'] = str(filament.nozzle_temp)
        filament_config['bed_temperature'] = str(filament.bed_temp)
        filament_config['print_speed'] = str(filament.print_speed)

        # Merge filament config into profile
        profile.update(filament_config)

        # Apply overrides
        profile['layer_height'] = str(layer_height)
        profile['infill_density'] = f"{infill_density}%"
        profile['support_material'] = "1" if supports else "0"

        return profile

    def prepare_workspace(self, job_id: str, objects: List[ObjectData]) -> Path:
        """Create sandbox workspace and copy normalized STLs."""
        workspace = Path(f"/cache/slicing/{job_id}")
        workspace.mkdir(parents=True, exist_ok=True)

        # Copy normalized STL files
        for obj in objects:
            src = Path(obj.normalized_path)
            if not src.exists():
                raise SlicingError(f"Normalized file not found: {obj.normalized_path}")

            dst = workspace / f"object_{obj.id}.stl"
            shutil.copy2(src, dst)

        return workspace

    def slice_bundle(
        self,
        workspace: Path,
        profile: Dict,
        output_name: str = "output.gcode"
    ) -> Dict:
        """Execute Orca Slicer CLI with Xvfb for headless slicing.

        Returns dict with:
        - success: bool
        - stdout: str
        - stderr: str
        - exit_code: int
        """
        # Find all STL files in workspace
        stl_files = sorted(workspace.glob("*.stl"))
        if not stl_files:
            raise SlicingError("No STL files found in workspace")

        # Build Orca command with printer, process, and filament settings
        # Configs are loaded from installed location (semicolon-separated)
        printer_config = "/root/.config/OrcaSlicer/user/machine/Snapmaker U1 (0.4 nozzle) - multiplate.json"
        process_config = "/root/.config/OrcaSlicer/user/process/0.20mm Standard @Snapmaker U1.json"
        filament_config = "/root/.config/OrcaSlicer/user/filament/PLA @Snapmaker U1.json"

        cmd = [
            "xvfb-run", "-a",
            str(self.orca_bin),
            "--slice", "0",  # Slice all plates
            "--load-settings", f"{printer_config};{process_config}",  # Load machine + process
            "--load-filaments", filament_config,  # Load filament settings
            "--outputdir", str(workspace)  # Output directory for G-code
        ]

        # Add STL files to slice
        cmd.extend([str(f) for f in stl_files])

        # Execute with timeout
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
                env={"DISPLAY": ":99"}
            )

            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode
            }
        except subprocess.TimeoutExpired:
            raise SlicingError("Slicing timed out after 5 minutes")
        except Exception as e:
            raise SlicingError(f"Slicing command failed: {str(e)}")

    def slice_3mf(
        self,
        three_mf_path: Path,
        workspace: Path,
        output_name: str = "output.gcode",
        plate_index: Optional[int] = None,
        scale_factor: Optional[float] = None,
        disable_arrange: bool = False,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        job_id: Optional[str] = None,
    ) -> Dict:
        """Execute Orca Slicer CLI with pre-built 3MF file.

        Args:
            three_mf_path: Path to 3MF file with embedded settings
            workspace: Working directory for output
            output_name: Output G-code filename (default: output.gcode)
            plate_index: Plate to slice (None = all)
            scale_factor: Scale factor (1.0 = no scaling)
            disable_arrange: If True, preserve authored placement (skip arrange/orient)
            progress_callback: Optional callback(percent, message) for real-time progress
            job_id: Optional job identifier for cancellation support

        Returns:
            Dict with success, stdout, stderr, exit_code
        """
        if not three_mf_path.exists():
            raise SlicingError(f"3MF file not found: {three_mf_path}")

        slice_arg = str(plate_index) if plate_index is not None else "0"

        cmd = [
            "xvfb-run", "-a",
            str(self.orca_bin),
            "--slice", slice_arg,
            "--allow-newer-file",
            "--ensure-on-bed",
            "--outputdir", str(workspace),
        ]
        if disable_arrange:
            # Preserve authored / user-edited placement for M33 transforms.
            cmd.extend(["--arrange", "0", "--orient", "0"])

        # Set up named pipe for real-time progress from OrcaSlicer
        pipe_path = None
        if progress_callback:
            pipe_path = str(workspace / f"progress_{os.getpid()}.pipe")
            try:
                os.mkfifo(pipe_path)
                cmd.extend(["--pipe", pipe_path])
            except OSError:
                pipe_path = None  # Fall back to no progress

        if scale_factor is not None and abs(float(scale_factor) - 1.0) > 1e-6:
            cmd.extend(["--scale", str(float(scale_factor))])
        cmd.append(str(three_mf_path))

        # Start pipe reader thread (reads JSON progress lines from OrcaSlicer)
        reader_thread = None
        if pipe_path:
            def _read_progress_pipe():
                try:
                    with open(pipe_path, "r") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                                pct = data.get("total_percent", 0)
                                msg = data.get("message", "")
                                progress_callback(pct, msg)
                            except (json.JSONDecodeError, TypeError):
                                pass
                except OSError:
                    pass

            reader_thread = threading.Thread(target=_read_progress_pipe, daemon=True)
            reader_thread.start()

        # Execute with Popen so the process can be cancelled via cancel_slice_job().
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"DISPLAY": ":99"},
        )
        if job_id:
            _active_processes[job_id] = proc

        try:
            stdout, stderr = proc.communicate(timeout=300)
            # Killed by cancel_slice_job() → negative return code on Linux
            if proc.returncode < 0 and job_id and job_id not in _active_processes:
                raise SlicingCancelledError("Slicing cancelled by user")
            return {
                "success": proc.returncode == 0,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": proc.returncode,
            }
        except SlicingCancelledError:
            raise
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise SlicingError("Slicing timed out after 5 minutes")
        except Exception as e:
            proc.kill()
            proc.wait(timeout=5)
            raise SlicingError(f"Slicing command failed: {str(e)}")
        finally:
            if job_id:
                _active_processes.pop(job_id, None)
            if reader_thread:
                reader_thread.join(timeout=3)
            if pipe_path:
                try:
                    os.unlink(pipe_path)
                except OSError:
                    pass

    async def slice_3mf_async(
        self,
        three_mf_path: Path,
        workspace: Path,
        output_name: str = "output.gcode",
        plate_index: Optional[int] = None,
        scale_factor: Optional[float] = None,
        disable_arrange: bool = False,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        job_id: Optional[str] = None,
    ) -> Dict:
        """Async version of slice_3mf — acquires semaphore to limit concurrent processes."""
        async with _get_slicer_semaphore():
            return await asyncio.to_thread(
                self.slice_3mf,
                three_mf_path,
                workspace,
                output_name,
                plate_index,
                scale_factor,
                disable_arrange,
                progress_callback,
                job_id,
            )

    def parse_gcode_metadata(self, gcode_path: Path) -> Dict:
        """Extract metadata from generated G-code."""
        metadata = parse_orca_metadata(gcode_path)

        return {
            "estimated_time_seconds": metadata.estimated_time_seconds,
            "filament_used_mm": metadata.filament_used_mm,
            "filament_used_g": metadata.filament_used_g,
            "layer_count": metadata.layer_count,
            "min_x": metadata.min_x,
            "min_y": metadata.min_y,
            "min_z": metadata.min_z,
            "max_x": metadata.max_x,
            "max_y": metadata.max_y,
            "max_z": metadata.max_z,
            "bounds": {
                "min_x": metadata.min_x,
                "min_y": metadata.min_y,
                "min_z": metadata.min_z,
                "max_x": metadata.max_x,
                "max_y": metadata.max_y,
                "max_z": metadata.max_z,
            }
        }

    def get_used_tools(self, gcode_path: Path) -> List[str]:
        """Return sorted list of used tool commands (T0, T1, ...)."""
        tool_re = re.compile(r"^T\d+$")
        used = set()
        with open(gcode_path, "r", errors="ignore") as f:
            for line in f:
                t = line.strip()
                if tool_re.match(t):
                    used.add(t)
        return sorted(used)

    def remap_compacted_tools(self, gcode_path: Path, target_tools: List[int]) -> Dict:
        """Remap compacted T0..Tn tools to desired tool IDs.

        Example: target_tools=[1,2] remaps T0->T1 and T1->T2.
        """
        if not target_tools:
            return {"applied": False, "reason": "no_target_tools"}

        with open(gcode_path, "r", errors="ignore") as f:
            lines = f.readlines()

        cmd_tool_re = re.compile(r"^\s*T(\d+)\s*$")
        used_numbers = []
        for line in lines:
            m = cmd_tool_re.match(line)
            if m:
                used_numbers.append(int(m.group(1)))

        if not used_numbers:
            return {"applied": False, "reason": "no_tool_lines"}

        compact = sorted(set(used_numbers))
        expected_compact = list(range(len(target_tools)))
        if compact != expected_compact:
            return {
                "applied": False,
                "reason": "non_compact_tools",
                "used": compact,
                "expected": expected_compact,
            }

        tool_map = {i: target_tools[i] for i in range(len(target_tools))}
        if all(src == dst for src, dst in tool_map.items()):
            return {"applied": False, "reason": "identity_map", "map": tool_map}

        m620_re = re.compile(r"^(\s*M620\s+S)(\d+)(A.*)$")
        m621_re = re.compile(r"^(\s*M621\s+S)(\d+)(A.*)$")
        t_param_re = re.compile(r"\bT(\d+)\b")

        rewritten = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(";"):
                rewritten.append(line)
                continue

            m_tool = cmd_tool_re.match(line)
            if m_tool:
                src_tool = int(m_tool.group(1))
                dst_tool = tool_map.get(src_tool, src_tool)
                rewritten.append(re.sub(r"T\d+", f"T{dst_tool}", line, count=1))
                continue

            s = line.strip()
            m620 = m620_re.match(s)
            if m620:
                src_tool = int(m620.group(2))
                dst_tool = tool_map.get(src_tool, src_tool)
                rewritten.append(f"{m620.group(1)}{dst_tool}{m620.group(3)}\n")
                continue

            m621 = m621_re.match(s)
            if m621:
                src_tool = int(m621.group(2))
                dst_tool = tool_map.get(src_tool, src_tool)
                rewritten.append(f"{m621.group(1)}{dst_tool}{m621.group(3)}\n")
                continue

            # Remap generic T-parameters in commands like M104/M109 ... Tn
            def _replace_t(match: re.Match) -> str:
                src_tool = int(match.group(1))
                dst_tool = tool_map.get(src_tool, src_tool)
                return f"T{dst_tool}"

            rewritten.append(t_param_re.sub(_replace_t, line))

        with open(gcode_path, "w") as f:
            f.writelines(rewritten)

        return {"applied": True, "map": tool_map}

    @staticmethod
    def _scan_xy_bounds(gcode_path: Path) -> Optional[Tuple[float, float, float, float]]:
        x = None
        y = None
        min_x = float("inf")
        max_x = float("-inf")
        min_y = float("inf")
        max_y = float("-inf")
        seen = False
        with open(gcode_path, "r", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(";"):
                    continue
                if not (line.startswith("G0") or line.startswith("G1")):
                    continue
                mx = re.search(r"\bX(-?\d+(?:\.\d+)?)", line)
                my = re.search(r"\bY(-?\d+(?:\.\d+)?)", line)
                if mx:
                    x = float(mx.group(1))
                if my:
                    y = float(my.group(1))
                if x is None or y is None:
                    continue
                seen = True
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
        if not seen:
            return None
        return (min_x, max_x, min_y, max_y)

    def validate_bounds(self, gcode_path: Path, expected_bounds: Optional[Dict] = None) -> bool:
        """Verify G-code movements stay within printer build volume.

        Args:
            gcode_path: Path to G-code file
            expected_bounds: Optional dict with expected object bounds

        Returns:
            True if bounds valid, raises SlicingError if validation fails
        """
        metadata = parse_orca_metadata(gcode_path)

        # Check against printer build volume
        if metadata.max_x > self.printer_profile.build_volume_x:
            raise SlicingError(
                f"Sliced G-code exceeds build volume: "
                f"X_max {metadata.max_x:.1f}mm > {self.printer_profile.build_volume_x}mm limit"
            )

        if metadata.max_y > self.printer_profile.build_volume_y:
            raise SlicingError(
                f"Sliced G-code exceeds build volume: "
                f"Y_max {metadata.max_y:.1f}mm > {self.printer_profile.build_volume_y}mm limit"
            )

        if metadata.max_z > self.printer_profile.build_volume_z:
            raise SlicingError(
                f"Sliced G-code exceeds build volume: "
                f"Z_max {metadata.max_z:.1f}mm > {self.printer_profile.build_volume_z}mm limit"
            )

        xy_bounds = self._scan_xy_bounds(gcode_path)
        if xy_bounds is not None:
            min_x, max_x_scan, min_y, max_y_scan = xy_bounds
            if min_x < -0.5:
                raise SlicingError(
                    f"Sliced G-code exceeds build volume: X_min {min_x:.1f}mm < 0mm limit"
                )
            if min_y < -0.5:
                raise SlicingError(
                    f"Sliced G-code exceeds build volume: Y_min {min_y:.1f}mm < 0mm limit"
                )
            # Keep consistency with metadata-based max checks; scan is a safety net.
            if max_x_scan > (self.printer_profile.build_volume_x + 0.5):
                raise SlicingError(
                    f"Sliced G-code exceeds build volume: X_max {max_x_scan:.1f}mm > {self.printer_profile.build_volume_x}mm limit"
                )
            if max_y_scan > (self.printer_profile.build_volume_y + 0.5):
                raise SlicingError(
                    f"Sliced G-code exceeds build volume: Y_max {max_y_scan:.1f}mm > {self.printer_profile.build_volume_y}mm limit"
                )

        return True

    def cleanup_workspace(self, workspace: Path):
        """Remove temporary workspace directory."""
        if workspace.exists():
            shutil.rmtree(workspace)
