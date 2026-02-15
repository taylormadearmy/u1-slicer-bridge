"""Profile embedding for 3MF files.

Embeds Orca Slicer profiles into existing 3MF files while preserving geometry.
Handles Bambu Studio files by extracting clean geometry with trimesh.
"""

import asyncio
import json
import zipfile
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import xml.etree.ElementTree as ET
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ProfileSettings:
    """Orca Slicer profile settings bundle."""
    printer: Dict[str, Any]
    process: Dict[str, Any]
    filament: Dict[str, Any]


class ProfileEmbedError(Exception):
    """Raised when profile embedding fails."""
    pass


class ProfileEmbedder:
    """Embeds Orca Slicer profiles into existing 3MF files."""

    def __init__(self, profile_dir: Path):
        """Initialize embedder with profile directory.

        Args:
            profile_dir: Directory containing orca_profiles/ with printer/process/filament JSONs
        """
        self.profile_dir = profile_dir
        logger.info(f"ProfileEmbedder initialized with profile_dir: {profile_dir}")

    def _is_bambu_file(self, three_mf_path: Path) -> bool:
        """Check if 3MF file is from Bambu Studio.

        Args:
            three_mf_path: Path to 3MF file

        Returns:
            True if file contains Bambu-specific metadata
        """
        try:
            with zipfile.ZipFile(three_mf_path, 'r') as zf:
                # Check for Bambu-specific files
                bambu_files = {
                    'Metadata/model_settings.config',
                    'Metadata/slice_info.config',
                    'Metadata/filament_sequence.json'
                }
                return bool(bambu_files & set(zf.namelist()))
        except Exception as e:
            logger.warning(f"Could not check if Bambu file: {e}")
            return False

    def _rebuild_with_trimesh(self, source_3mf: Path, dest_3mf: Path) -> None:
        """Rebuild 3MF with trimesh to extract clean geometry.

        This strips Bambu-specific format issues and creates a clean 3MF
        that Orca Slicer can parse.

        Args:
            source_3mf: Original Bambu 3MF path
            dest_3mf: Output clean 3MF path
        """
        try:
            import trimesh
            logger.info(f"Rebuilding Bambu 3MF with trimesh: {source_3mf.name}")

            # Load entire scene (preserves object positions)
            scene = trimesh.load(str(source_3mf), file_type='3mf')

            # Export as clean 3MF
            scene.export(str(dest_3mf), file_type='3mf')

            logger.info(f"Rebuilt clean 3MF: {dest_3mf.name} ({dest_3mf.stat().st_size / 1024 / 1024:.2f} MB)")

        except ImportError:
            raise ProfileEmbedError("trimesh library not installed - cannot process Bambu files")
        except Exception as e:
            raise ProfileEmbedError(f"Failed to rebuild 3MF with trimesh: {str(e)}")

    def _has_multi_extruder_assignments(self, three_mf_path: Path) -> bool:
        """Check if model_settings.config contains multiple extruder assignments.

        Returns True when the 3MF has explicit per-object/per-part extruder mapping
        for more than one extruder. In that case we should preserve original metadata
        (no trimesh rebuild), otherwise assignments are lost and slicing becomes single-tool.
        """
        try:
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(three_mf_path, 'r') as zf:
                if 'Metadata/model_settings.config' not in zf.namelist():
                    return False

                root = ET.fromstring(zf.read('Metadata/model_settings.config'))
                extruders = set()

                for meta in root.findall('.//metadata'):
                    if meta.get('key') == 'extruder':
                        v = meta.get('value')
                        if v is not None:
                            extruders.add(v)

                return len(extruders) > 1
        except Exception as e:
            logger.debug(f"Could not detect multi-extruder assignments: {e}")
            return False

    @staticmethod
    def _has_layer_tool_changes(three_mf_path: Path) -> bool:
        """Check if custom_gcode_per_layer.xml contains ToolChange entries.

        Bambu's MultiAsSingle mode stores mid-print filament swaps as
        type="2" entries.  These must be preserved (not stripped by trimesh
        rebuild) so OrcaSlicer can emit real T-commands.
        """
        try:
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(three_mf_path, 'r') as zf:
                if 'Metadata/custom_gcode_per_layer.xml' not in zf.namelist():
                    return False
                root = ET.fromstring(zf.read('Metadata/custom_gcode_per_layer.xml'))
                for layer in root.findall('.//layer'):
                    if layer.get('type') == '2':
                        return True
            return False
        except Exception:
            return False

    def _get_assigned_extruder_count(self, three_mf_path: Path) -> int:
        """Get highest assigned extruder index from model_settings.config."""
        try:
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(three_mf_path, 'r') as zf:
                if 'Metadata/model_settings.config' not in zf.namelist():
                    return 1

                root = ET.fromstring(zf.read('Metadata/model_settings.config'))
                max_idx = 1
                for meta in root.findall('.//metadata'):
                    if meta.get('key') != 'extruder':
                        continue
                    raw = meta.get('value')
                    if raw is None:
                        continue
                    try:
                        idx = int(str(raw).strip())
                    except ValueError:
                        continue
                    if idx > max_idx:
                        max_idx = idx
                return max_idx
        except Exception as e:
            logger.debug(f"Could not parse assigned extruder count: {e}")
            return 1

    @staticmethod
    def _ensure_list(value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @staticmethod
    def _pad_list(values: List[Any], target_len: int, default_value: Any) -> List[Any]:
        if target_len <= 0:
            return values
        if not values:
            values = [default_value]
        padded = list(values)
        while len(padded) < target_len:
            padded.append(padded[-1])
        return padded

    @staticmethod
    def _sanitize_index_field(config: Dict[str, Any], key: str, minimum: int) -> None:
        raw = config.get(key)
        if raw is None:
            return
        try:
            numeric = int(float(str(raw).strip()))
        except Exception:
            numeric = minimum
        if numeric < minimum:
            numeric = minimum
        config[key] = str(numeric)

    @staticmethod
    def _sanitize_float_field(config: Dict[str, Any], key: str, minimum: float) -> None:
        raw = config.get(key)
        if raw is None:
            return

        # Bambu settings may encode numeric fields as a scalar or single-item list.
        value = raw[0] if isinstance(raw, list) and raw else raw
        try:
            numeric = float(str(value).strip())
        except Exception:
            return

        if numeric < minimum:
            numeric = minimum

        normalized = str(numeric)
        if isinstance(raw, list):
            config[key] = [normalized]
        else:
            config[key] = normalized

    @staticmethod
    def _get_numeric(config: Dict[str, Any], key: str, fallback: float) -> float:
        raw = config.get(key)
        if raw is None:
            return fallback
        value = raw[0] if isinstance(raw, list) and raw else raw
        try:
            return float(str(value).strip())
        except Exception:
            return fallback

    def _sanitize_wipe_tower_position(
        self,
        config: Dict[str, Any],
        bed_size_mm: float = 270.0,
        extra_margin_mm: float = 6.0,
    ) -> None:
        """Keep wipe/prime tower safely inside bed bounds.

        Orca/Bambu configs can carry low `wipe_tower_x` values (e.g., 15mm) that
        place a wide prime tower at or beyond the printable edge.
        """
        tower_width = self._get_numeric(config, 'prime_tower_width', 35.0)
        tower_brim = max(0.0, self._get_numeric(config, 'prime_tower_brim_width', 3.0))
        half_span = max(12.0, (tower_width / 2.0) + tower_brim + extra_margin_mm)
        min_pos = half_span
        max_pos = max(min_pos, bed_size_mm - half_span)

        for axis_key in ('wipe_tower_x', 'wipe_tower_y'):
            raw = config.get(axis_key)
            if raw is None:
                continue

            value = raw[0] if isinstance(raw, list) and raw else raw
            try:
                numeric = float(str(value).strip())
            except Exception:
                continue

            if numeric < min_pos:
                numeric = min_pos
            if numeric > max_pos:
                numeric = max_pos

            normalized = f"{numeric:.3f}"
            if isinstance(raw, list):
                config[axis_key] = [normalized]
            else:
                config[axis_key] = normalized

    def _build_assignment_preserving_config(
        self,
        source_3mf: Path,
        profiles: ProfileSettings,
        filament_settings: Dict[str, Any],
        overrides: Dict[str, Any],
        requested_filament_count: int,
    ) -> Dict[str, Any]:
        """Build config that preserves Bambu object->extruder assignments."""
        with zipfile.ZipFile(source_3mf, 'r') as zf:
            if 'Metadata/project_settings.config' in zf.namelist():
                base_config = json.loads(zf.read('Metadata/project_settings.config'))
            else:
                base_config = {}

        config: Dict[str, Any] = dict(base_config)

        for key in (
            'before_layer_change_gcode',
            'layer_change_gcode',
            'change_filament_gcode',
            'machine_start_gcode',
            'machine_end_gcode',
            'gcode_flavor',
        ):
            if key in profiles.printer:
                config[key] = profiles.printer[key]

        for key in ('time_lapse_gcode', 'machine_pause_gcode'):
            config.pop(key, None)

        config.update(profiles.process)
        config.update(profiles.filament)
        config.update(filament_settings)
        config.update(overrides)

        config['layer_gcode'] = 'G92 E0'
        config.setdefault('enable_arc_fitting', '1')

        self._sanitize_index_field(config, 'raft_first_layer_expansion', 0)
        self._sanitize_index_field(config, 'tree_support_wall_count', 0)
        self._sanitize_index_field(config, 'prime_volume', 0)
        self._sanitize_index_field(config, 'prime_tower_brim_width', 0)
        self._sanitize_index_field(config, 'prime_tower_brim_chamfer', 0)
        self._sanitize_index_field(config, 'prime_tower_brim_chamfer_max_width', 0)
        # Keep purge/prime tower safely inside bed bounds for U1 reliability.
        self._sanitize_wipe_tower_position(config)
        self._sanitize_index_field(config, 'solid_infill_filament', 1)
        self._sanitize_index_field(config, 'sparse_infill_filament', 1)
        self._sanitize_index_field(config, 'wall_filament', 1)

        assigned_count = self._get_assigned_extruder_count(source_3mf)
        target_slots = max(assigned_count, requested_filament_count, 1)

        list_defaults = {
            'filament_type': ['PLA'],
            'filament_colour': ['#FFFFFF'],
            'extruder_colour': ['#FFFFFF'],
            'default_filament_profile': ['Snapmaker PLA'],
            'filament_settings_id': ['Snapmaker PLA'],
            'nozzle_temperature': ['210'],
            'nozzle_temperature_initial_layer': ['210'],
            'bed_temperature': ['60'],
            'bed_temperature_initial_layer': ['60'],
            'cool_plate_temp': ['60'],
            'cool_plate_temp_initial_layer': ['60'],
            'textured_plate_temp': ['60'],
            'textured_plate_temp_initial_layer': ['60'],
        }

        for key, fallback in list_defaults.items():
            values = self._ensure_list(config.get(key))
            if not values:
                values = list(fallback)
            config[key] = self._pad_list(values, target_slots, fallback[-1])

        bed_single = self._ensure_list(config.get('bed_temperature_initial_layer_single'))
        if not bed_single:
            bed_single = [config['bed_temperature_initial_layer'][0]]
        config['bed_temperature_initial_layer_single'] = bed_single

        # U1 workflow targets explicit extruder slots (E1-E4), not single-nozzle MMU swaps.
        # Keep this off to avoid inflated load/unload toolchange behavior and time estimates.
        config['single_extruder_multi_material'] = '0'

        logger.info(
            "Built assignment-preserving config with %s extruder slots "
            "(requested=%s, assigned=%s)",
            target_slots,
            requested_filament_count,
            assigned_count,
        )
        return config

    def embed_profiles(self,
                       source_3mf: Path,
                       output_3mf: Path,
                       filament_settings: Dict[str, Any],
                       overrides: Dict[str, Any],
                       requested_filament_count: int = 1,
                       extruder_remap: Dict[int, int] | None = None,
                       preserve_geometry: bool = False,
                       precomputed_is_bambu: Optional[bool] = None,
                       precomputed_has_multi_assignments: Optional[bool] = None,
                       precomputed_has_layer_changes: Optional[bool] = None) -> Path:
        """Copy original 3MF and inject Orca profiles.

        Preserves all original geometry, transforms, and positioning.
        Only adds/updates the Metadata/project_settings.config file.

        For Bambu Studio files, extracts clean geometry with trimesh first.

        Args:
            source_3mf: Path to original 3MF file
            output_3mf: Path where modified 3MF should be saved
            filament_settings: Filament-specific settings (temps, speeds, etc.)
            overrides: User-specified settings (layer_height, infill_density, etc.)

        Returns:
            Path to output 3MF file

        Raises:
            ProfileEmbedError: If embedding fails
        """
        working_3mf = source_3mf
        try:
            logger.info(f"Embedding profiles into {source_3mf.name}")

            profiles = self.load_snapmaker_profiles()

            # Check if this is a Bambu file that needs rebuilding
            # Use precomputed values when available (avoids redundant ZIP opens)
            preserve_model_settings_from = None
            is_bambu = precomputed_is_bambu if precomputed_is_bambu is not None else self._is_bambu_file(source_3mf)
            has_multi_assignments = precomputed_has_multi_assignments if precomputed_has_multi_assignments is not None else self._has_multi_extruder_assignments(source_3mf)
            has_layer_changes = precomputed_has_layer_changes if precomputed_has_layer_changes is not None else self._has_layer_tool_changes(source_3mf)

            # Use assignment-preserving path for either per-object multicolor
            # OR layer-based tool changes (MultiAsSingle dual-colour).
            # Layer tool changes live in custom_gcode_per_layer.xml which
            # the trimesh rebuild path would destroy.
            needs_preserve = has_multi_assignments or has_layer_changes
            if is_bambu and requested_filament_count > 1 and needs_preserve:
                reason = (
                    "layer-based tool changes" if has_layer_changes
                    else "model extruder assignments"
                )
                logger.info(
                    f"Detected Bambu multicolor file with {reason} - "
                    "preserving original geometry and metadata"
                )
                config = self._build_assignment_preserving_config(
                    source_3mf=source_3mf,
                    profiles=profiles,
                    filament_settings=filament_settings,
                    overrides=overrides,
                    requested_filament_count=requested_filament_count,
                )
                settings_json = json.dumps(config, indent=2)
                self._copy_and_inject_settings(
                    source_3mf,
                    output_3mf,
                    settings_json,
                    preserve_model_settings_from=None,
                    extruder_remap=extruder_remap,
                )
                logger.info(f"Successfully embedded profiles into {output_3mf.name}")
                return output_3mf

            if is_bambu and not preserve_geometry:
                logger.info("Detected Bambu Studio file - rebuilding with trimesh")
                temp_clean = source_3mf.parent / f"{source_3mf.stem}_clean.3mf"
                self._rebuild_with_trimesh(source_3mf, temp_clean)
                working_3mf = temp_clean

            # Merge all settings
            config = {
                **profiles.printer,
                **profiles.process,
                **profiles.filament,
                **filament_settings,
                **overrides
            }

            # Ensure layer_gcode for relative extruder addressing
            if 'layer_gcode' not in config:
                config['layer_gcode'] = 'G92 E0'

            # Ensure arc fitting to reduce G-code file size
            if 'enable_arc_fitting' not in config:
                config['enable_arc_fitting'] = '1'

            logger.debug(f"Merged config with {len(config)} keys")

            # Create JSON settings
            settings_json = json.dumps(config, indent=2)

            # Copy and modify 3MF (use working_3mf which may be cleaned version)
            self._copy_and_inject_settings(
                working_3mf,
                output_3mf,
                settings_json,
                preserve_model_settings_from=preserve_model_settings_from,
                extruder_remap=extruder_remap,
            )

            # Clean up temporary clean 3MF if we created one
            if working_3mf != source_3mf and working_3mf.exists():
                working_3mf.unlink()
                logger.debug(f"Cleaned up temporary file: {working_3mf.name}")

            logger.info(f"Successfully embedded profiles into {output_3mf.name}")
            return output_3mf

        except Exception as e:
            # Clean up temporary files on error
            temp_working = locals().get('working_3mf')
            if isinstance(temp_working, Path) and temp_working != source_3mf and temp_working.exists():
                temp_working.unlink()
            logger.error(f"Failed to embed profiles: {str(e)}")
            raise ProfileEmbedError(f"Profile embedding failed: {str(e)}") from e

    async def embed_profiles_async(self, **kwargs) -> Path:
        """Async version of embed_profiles — runs in thread pool to avoid blocking the event loop."""
        return await asyncio.to_thread(self.embed_profiles, **kwargs)

    def _copy_and_inject_settings(
        self,
        source: Path,
        dest: Path,
        settings_json: str,
        preserve_model_settings_from: Path | None = None,
        extruder_remap: Dict[int, int] | None = None,
    ):
        """Copy 3MF and add/update project_settings.config.

        Args:
            source: Source 3MF path
            dest: Destination 3MF path
            settings_json: JSON string to write to Metadata/project_settings.config
        """
        # Create temporary ZIP for rebuilding
        temp_zip = dest.with_suffix('.tmp')

        try:
            # Bambu Studio metadata files that are safe to drop
            bambu_metadata_files = {
                'Metadata/project_settings.config',  # We'll replace this
                'Metadata/slice_info.config',        # Bambu-specific
                'Metadata/cut_information.xml',      # Bambu-specific
                'Metadata/filament_sequence.json',   # Bambu-specific (can crash Orca)
            }

            with zipfile.ZipFile(source, 'r') as source_zf:
                with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as dest_zf:
                    # Copy geometry and essential files, skip Bambu metadata
                    for item in source_zf.infolist():
                        if item.filename in bambu_metadata_files:
                            logger.debug(f"Skipping Bambu metadata: {item.filename}")
                            continue

                        # Skip Bambu preview images to reduce file size
                        if item.filename.startswith('Metadata/plate') or item.filename.startswith('Metadata/top') or item.filename.startswith('Metadata/pick'):
                            logger.debug(f"Skipping preview image: {item.filename}")
                            continue

                        # Copy file as-is (geometry, relations, etc.)
                        data = source_zf.read(item.filename)
                        if item.filename == 'Metadata/model_settings.config':
                            data = self._sanitize_model_settings(data, extruder_remap=extruder_remap)
                        dest_zf.writestr(item, data)

                    # Add new project_settings.config
                    dest_zf.writestr('Metadata/project_settings.config', settings_json)
                    logger.debug("Injected new project_settings.config")

                    # Optionally preserve model_settings.config from original 3MF
                    if preserve_model_settings_from is not None and preserve_model_settings_from != source:
                        try:
                            with zipfile.ZipFile(preserve_model_settings_from, 'r') as original_zf:
                                if 'Metadata/model_settings.config' in original_zf.namelist():
                                    model_settings = original_zf.read('Metadata/model_settings.config')
                                    model_settings = self._sanitize_model_settings(model_settings, extruder_remap=extruder_remap)
                                    dest_zf.writestr('Metadata/model_settings.config', model_settings)
                                    logger.debug("Preserved Metadata/model_settings.config from original file")
                        except Exception as e:
                            logger.warning(f"Could not preserve model_settings.config: {e}")

            # Replace destination with temp file
            temp_zip.replace(dest)

        except Exception as e:
            # Clean up temp file on error
            if temp_zip.exists():
                temp_zip.unlink()
            raise

    @staticmethod
    def _sanitize_model_settings(
        model_settings_bytes: bytes,
        extruder_remap: Dict[int, int] | None = None,
    ) -> bytes:
        """Sanitize model_settings metadata known to trigger Orca instability.

        Some Bambu exports keep stale plate names in `plater_name` when objects are
        moved between/deleted plates. Snapmaker Orca v2.2.4 can segfault on those.
        """
        try:
            root = ET.fromstring(model_settings_bytes)
            changed = False
            for meta in root.findall('.//metadata'):
                if meta.get('key') == 'plater_name' and (meta.get('value') or ''):
                    meta.set('value', '')
                    changed = True

                if extruder_remap and meta.get('key') == 'extruder':
                    raw = (meta.get('value') or '').strip()
                    if raw.isdigit():
                        src_ext = int(raw)
                        dst_ext = extruder_remap.get(src_ext)
                        if dst_ext is not None and 1 <= dst_ext <= 4 and dst_ext != src_ext:
                            meta.set('value', str(dst_ext))
                            changed = True

            if changed:
                logger.info("Sanitized model_settings metadata")
                return ET.tostring(root, encoding='utf-8', xml_declaration=True)
        except Exception as e:
            logger.warning(f"Could not sanitize model_settings.config: {e}")

        return model_settings_bytes

    # Module-level profile cache (profiles are static within a Docker image)
    _cached_profiles: Optional[ProfileSettings] = None
    _cached_profiles_dir: Optional[Path] = None

    def load_snapmaker_profiles(self) -> ProfileSettings:
        """Load default Snapmaker U1 profiles from JSON files.

        Returns cached profiles on subsequent calls (files are static in Docker).

        Returns:
            ProfileSettings with printer, process, and filament configs

        Raises:
            ProfileEmbedError: If profiles cannot be loaded
        """
        if (ProfileEmbedder._cached_profiles is not None
                and ProfileEmbedder._cached_profiles_dir == self.profile_dir):
            return ProfileEmbedder._cached_profiles

        try:
            printer_path = self.profile_dir / "printer" / "Snapmaker U1 (0.4 nozzle) - multiplate.json"
            process_path = self.profile_dir / "process" / "0.20mm Standard @Snapmaker U1.json"
            filament_path = self.profile_dir / "filament" / "PLA @Snapmaker U1.json"

            with open(printer_path) as f:
                printer = json.load(f)
            with open(process_path) as f:
                process = json.load(f)
            with open(filament_path) as f:
                filament = json.load(f)

            logger.info(f"Loaded and cached profiles: {printer_path.name}, {process_path.name}, {filament_path.name}")

            ProfileEmbedder._cached_profiles = ProfileSettings(printer=printer, process=process, filament=filament)
            ProfileEmbedder._cached_profiles_dir = self.profile_dir
            return ProfileEmbedder._cached_profiles

        except FileNotFoundError as e:
            raise ProfileEmbedError(f"Profile file not found: {e.filename}")
        except json.JSONDecodeError as e:
            raise ProfileEmbedError(f"Invalid JSON in profile: {str(e)}")
