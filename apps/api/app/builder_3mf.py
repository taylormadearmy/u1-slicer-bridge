"""Programmatic 3MF file creation with embedded Orca Slicer profiles.

This module enables fully automated 3MF creation from normalized STL files
with embedded Snapmaker U1 printer, process, and filament settings.
"""

import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import trimesh
import logging

logger = logging.getLogger(__name__)


class ThreeMFBuildError(Exception):
    """Raised when 3MF file creation fails."""
    pass


@dataclass
class ObjectMeshData:
    """3D object mesh data for 3MF embedding."""
    id: int
    name: str
    stl_path: Path


@dataclass
class ProfileSettings:
    """Orca Slicer profile settings bundle."""
    printer: Dict[str, Any]
    process: Dict[str, Any]
    filament: Dict[str, Any]


class ThreeMFBuilder:
    """Build 3MF files with embedded Snapmaker U1 profiles.

    This builder creates valid 3MF files (ZIP archives) containing:
    - 3D model geometry from normalized STL files
    - Embedded Orca Slicer printer/process/filament settings
    - Proper 3MF structure with XML namespaces and relationships
    """

    # 3MF namespace
    NAMESPACE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

    def __init__(self, profile_dir: Path):
        """Initialize builder with path to Orca Slicer profiles.

        Args:
            profile_dir: Directory containing orca_profiles/ with printer/process/filament JSONs
        """
        self.profile_dir = profile_dir
        logger.info(f"ThreeMFBuilder initialized with profile_dir: {profile_dir}")

    def build_bundle_3mf(
        self,
        objects: List[ObjectMeshData],
        output_path: Path,
        settings_overrides: Optional[Dict[str, Any]] = None
    ) -> Path:
        """Main entry point: create 3MF from normalized STLs.

        Args:
            objects: List of normalized STL objects to include
            output_path: Where to save the 3MF file
            settings_overrides: Optional dict to override specific settings
                Example: {"layer_height": "0.16", "infill_density": "20%"}

        Returns:
            Path to created 3MF file

        Raises:
            ThreeMFBuildError: If 3MF creation fails
        """
        if not objects:
            raise ThreeMFBuildError("Cannot create 3MF: no objects provided")

        logger.info(f"Building 3MF for {len(objects)} object(s) to {output_path}")

        try:
            # Load Snapmaker U1 profiles
            profiles = self.load_snapmaker_profiles()

            # Create ZIP archive
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Add content types
                zf.writestr('[Content_Types].xml', self.create_content_types_xml())

                # Add relationships
                zf.writestr('_rels/.rels', self.create_relationships_xml())

                # Add model with geometry
                model_xml = self.create_model_xml(objects)
                zf.writestr('3D/3dmodel.model', model_xml)

                # Add settings
                settings_str = self.create_project_settings_config(
                    profiles,
                    settings_overrides or {}
                )
                zf.writestr('Metadata/project_settings.config', settings_str)

            # Validate created 3MF
            self.validate_3mf(output_path)

            file_size_mb = output_path.stat().st_size / 1024 / 1024
            logger.info(f"3MF created successfully: {output_path} ({file_size_mb:.2f} MB)")

            return output_path

        except Exception as e:
            logger.error(f"Failed to build 3MF: {str(e)}")
            raise ThreeMFBuildError(f"3MF creation failed: {str(e)}") from e

    def load_snapmaker_profiles(self) -> ProfileSettings:
        """Load default Snapmaker U1 profiles from JSON files.

        Returns:
            ProfileSettings with printer, process, and filament configs

        Raises:
            ThreeMFBuildError: If profiles cannot be loaded
        """
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

            logger.debug(f"Loaded profiles: {printer_path.name}, {process_path.name}, {filament_path.name}")

            return ProfileSettings(printer=printer, process=process, filament=filament)

        except FileNotFoundError as e:
            raise ThreeMFBuildError(f"Profile file not found: {e.filename}")
        except json.JSONDecodeError as e:
            raise ThreeMFBuildError(f"Invalid JSON in profile: {str(e)}")

    def create_content_types_xml(self) -> str:
        """Generate [Content_Types].xml for 3MF package.

        Returns:
            XML string with MIME type declarations
        """
        root = ET.Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")

        # Default type handlers
        ET.SubElement(root, "Default", Extension="rels",
                     ContentType="application/vnd.openxmlformats-package.relationships+xml")
        ET.SubElement(root, "Default", Extension="xml",
                     ContentType="application/xml")

        # Override for model file
        ET.SubElement(root, "Override", PartName="/3D/3dmodel.model",
                     ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml")

        # Override for metadata
        ET.SubElement(root, "Override", PartName="/Metadata/project_settings.config",
                     ContentType="text/plain")

        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode')

    def create_relationships_xml(self) -> str:
        """Generate _rels/.rels for 3MF package relationships.

        Returns:
            XML string defining package relationships
        """
        root = ET.Element("Relationships",
                         xmlns="http://schemas.openxmlformats.org/package/2006/relationships")

        ET.SubElement(root, "Relationship",
                     Id="rel0",
                     Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
                     Target="/3D/3dmodel.model")

        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode')

    def create_model_xml(self, objects: List[ObjectMeshData]) -> str:
        """Generate 3D/3dmodel.model with mesh geometry.

        Args:
            objects: List of objects to include in the model

        Returns:
            XML string with 3MF model structure

        Raises:
            ThreeMFBuildError: If mesh loading or conversion fails
        """
        # Create root with namespace
        root = ET.Element("model",
                         xmlns=self.NAMESPACE,
                         unit="millimeter")

        resources = ET.SubElement(root, "resources")
        build = ET.SubElement(root, "build")

        for i, obj_data in enumerate(objects, start=1):
            try:
                # Load STL with trimesh
                logger.debug(f"Loading STL: {obj_data.stl_path}")
                mesh = trimesh.load(str(obj_data.stl_path))

                # Convert to 3MF mesh XML
                object_elem = ET.SubElement(resources, "object",
                                           id=str(i),
                                           name=obj_data.name,
                                           type="model")
                mesh_elem = self.stl_to_3mf_mesh(mesh, i)
                object_elem.append(mesh_elem)

                # Add to build
                ET.SubElement(build, "item", objectid=str(i))

                logger.debug(f"Added object {i}: {obj_data.name} ({len(mesh.vertices)} vertices)")

            except Exception as e:
                raise ThreeMFBuildError(f"Failed to process object {obj_data.name}: {str(e)}")

        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding='unicode')

    def stl_to_3mf_mesh(self, mesh: trimesh.Trimesh, object_id: int) -> ET.Element:
        """Convert trimesh STL to 3MF mesh XML element.

        Args:
            mesh: Trimesh object loaded from STL
            object_id: Object ID for logging

        Returns:
            XML Element containing mesh with vertices and triangles
        """
        mesh_elem = ET.Element("mesh")

        # Add vertices
        vertices_elem = ET.SubElement(mesh_elem, "vertices")
        for vertex in mesh.vertices:
            ET.SubElement(vertices_elem, "vertex",
                         x=f"{vertex[0]:.6f}",
                         y=f"{vertex[1]:.6f}",
                         z=f"{vertex[2]:.6f}")

        # Add triangles (faces)
        triangles_elem = ET.SubElement(mesh_elem, "triangles")
        for face in mesh.faces:
            ET.SubElement(triangles_elem, "triangle",
                         v1=str(face[0]),
                         v2=str(face[1]),
                         v3=str(face[2]))

        return mesh_elem

    def create_project_settings_config(
        self,
        profiles: ProfileSettings,
        overrides: Dict[str, Any]
    ) -> str:
        """Generate Metadata/project_settings.config (JSON format).

        Args:
            profiles: Printer, process, and filament settings
            overrides: User-specified settings to override defaults

        Returns:
            JSON config string
        """
        # Start with merged profile data
        config = {**profiles.printer, **profiles.process, **profiles.filament}

        # Ensure layer_gcode includes G92 E0 for relative extruder addressing
        if 'layer_gcode' not in config:
            config['layer_gcode'] = 'G92 E0'

        # Apply user overrides (convert to appropriate types)
        for key, value in overrides.items():
            config[key] = value

        logger.debug(f"Generated JSON config with {len(config)} keys")

        # Return as JSON (Orca expects JSON format, not INI)
        return json.dumps(config, indent=2)

    def json_profiles_to_config_format(self, profiles: ProfileSettings) -> Dict[str, str]:
        """Flatten nested JSON profiles to key=value pairs.

        Args:
            profiles: ProfileSettings with nested JSON structures

        Returns:
            Dict with flattened key=value pairs suitable for INI format
        """
        config = {}

        # Merge all profiles
        all_settings = {**profiles.printer, **profiles.process, **profiles.filament}

        # Skip only internal metadata fields, but KEEP compatibility metadata
        skip_keys = {'type', 'from', 'inherits', 'instantiation', 'version',
                     'is_custom_defined', 'printer_notes'}
        # REMOVED: 'name', 'compatible_printers_condition' to preserve compatibility metadata

        for key, value in all_settings.items():
            if key in skip_keys:
                continue

            # Handle arrays
            if isinstance(value, list):
                if len(value) == 0:
                    continue
                # For string arrays, join with semicolon (Orca's array format)
                if all(isinstance(x, str) for x in value):
                    config[key] = ';'.join(value)
                else:
                    # Multi-extruder arrays - use first value for single extruder
                    config[key] = str(value[0])
            else:
                config[key] = str(value)

        # Explicitly ensure critical profile identifier fields are present
        # These help Orca validate the profile configuration
        if 'printer_settings_id' not in config and 'name' in profiles.printer:
            config['printer_settings_id'] = profiles.printer['name']
        if 'print_settings_id' not in config and 'name' in profiles.process:
            config['print_settings_id'] = profiles.process['name']
        if 'filament_settings_id' not in config and 'name' in profiles.filament:
            config['filament_settings_id'] = profiles.filament['name']

        logger.debug(f"Generated config with {len(config)} keys including compatibility metadata")

        return config

    def validate_3mf(self, three_mf_path: Path) -> bool:
        """Validate 3MF structure after creation.

        Args:
            three_mf_path: Path to 3MF file to validate

        Returns:
            True if valid

        Raises:
            ThreeMFBuildError: If validation fails
        """
        try:
            with zipfile.ZipFile(three_mf_path, 'r') as zf:
                required_files = [
                    '[Content_Types].xml',
                    '_rels/.rels',
                    '3D/3dmodel.model'
                ]

                for f in required_files:
                    if f not in zf.namelist():
                        raise ThreeMFBuildError(f"Missing required file: {f}")

                # Validate XML structure
                model_xml = zf.read('3D/3dmodel.model')
                root = ET.fromstring(model_xml)

                # Check namespace
                if self.NAMESPACE not in root.tag:
                    raise ThreeMFBuildError("Invalid 3MF namespace")

                logger.debug("3MF validation passed")
                return True

        except zipfile.BadZipFile:
            raise ThreeMFBuildError("Invalid ZIP archive")
        except ET.ParseError as e:
            raise ThreeMFBuildError(f"Invalid XML: {str(e)}")
