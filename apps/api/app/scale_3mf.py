"""3MF scaling utilities."""

from __future__ import annotations

import shutil
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL_SETTINGS_PATH = "Metadata/model_settings.config"
INT_FMT = "{:.6f}"


def _fmt(v: float) -> str:
    text = INT_FMT.format(v).rstrip("0").rstrip(".")
    return text if text else "0"


def _scale_transform(transform: str, scale_factor: float, scale_translation: bool = False) -> str:
    try:
        values = [float(v) for v in transform.split()]
    except Exception:
        return transform
    if len(values) != 12:
        return transform
    # Always scale basis vectors.
    for idx in range(9):
        values[idx] *= scale_factor
    # Optionally scale translation terms for nested assembly/component offsets.
    if scale_translation:
        for idx in (9, 10, 11):
            values[idx] *= scale_factor
    return " ".join(_fmt(v) for v in values)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _scale_model_xml(data: bytes, scale_factor: float) -> bytes:
    root = ET.fromstring(data)
    # Scale top-level build items for object size and also scale nested
    # component transforms so intra-assembly spacing stays proportional.
    for elem in root.iter():
        name = _local_name(elem.tag)
        if name == "item":
            current = elem.get("transform")
            if current:
                elem.set(
                    "transform",
                    _scale_transform(current, scale_factor, scale_translation=False),
                )
            else:
                elem.set(
                    "transform",
                    f"{_fmt(scale_factor)} 0 0 0 {_fmt(scale_factor)} 0 0 0 {_fmt(scale_factor)} 0 0 0",
                )
        elif name == "component":
            current = elem.get("transform")
            if current:
                elem.set(
                    "transform",
                    _scale_component_translation_only(current, scale_factor),
                )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _scale_model_settings_xml(data: bytes, scale_factor: float) -> bytes:
    # Keep model_settings untouched. Orca can apply those transforms in
    # addition to 3D/3dmodel.model, which causes double-scaling on some files.
    return data


def _scale_component_translation_only(transform: str, scale_factor: float) -> str:
    try:
        values = [float(v) for v in transform.split()]
    except Exception:
        return transform
    if len(values) != 12:
        return transform
    for idx in (9, 10, 11):
        values[idx] *= scale_factor
    return " ".join(_fmt(v) for v in values)


def _scale_matrix_translation_only(text: str, scale_factor: float) -> str:
    parts = text.split()
    if len(parts) != 16:
        return text
    try:
        values = [float(v) for v in parts]
    except Exception:
        return text
    for idx in (3, 7, 11):
        values[idx] *= scale_factor
    return " ".join(_fmt(v) for v in values)


def _scale_component_offsets_model_xml(data: bytes, scale_factor: float) -> bytes:
    root = ET.fromstring(data)
    for elem in root.iter():
        if _local_name(elem.tag) != "component":
            continue
        transform = elem.get("transform")
        if transform:
            elem.set("transform", _scale_component_translation_only(transform, scale_factor))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _scale_component_offsets_model_settings_xml(data: bytes, scale_factor: float) -> bytes:
    root = ET.fromstring(data)
    for elem in root.iter():
        if _local_name(elem.tag) != "metadata":
            continue
        if elem.get("key") != "matrix":
            continue
        value = elem.get("value")
        if value:
            elem.set("value", _scale_matrix_translation_only(value, scale_factor))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def apply_uniform_scale_to_3mf(source_3mf: Path, output_3mf: Path, scale_percent: float) -> None:
    """Apply uniform build-item scaling to all objects in a 3MF.

    Scaling is applied by adjusting transforms in `3D/3dmodel.model`.
    Top-level build item translations are preserved to keep plate placement
    stable, while nested component offsets are scaled with geometry.
    """
    if abs(scale_percent - 100.0) < 0.001:
        shutil.copy2(source_3mf, output_3mf)
        return

    scale_factor = float(scale_percent) / 100.0
    ET.register_namespace("", CORE_NS)

    with zipfile.ZipFile(source_3mf, "r") as zin, zipfile.ZipFile(
        output_3mf, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)

            if info.filename.endswith(".model"):
                data = _scale_model_xml(data, scale_factor)
            elif info.filename == MODEL_SETTINGS_PATH:
                data = _scale_model_settings_xml(data, scale_factor)

            zout.writestr(info, data)


def apply_layout_scale_to_3mf(source_3mf: Path, output_3mf: Path, scale_percent: float) -> None:
    """Scale only intra-assembly offsets so native slicer scaling can remain enabled.

    Snapmaker Orca's `--scale` can scale part geometry but miss component/matrix
    offset spacing in some Bambu-style assemblies. This patch scales those
    offsets in both `3D/3dmodel.model` component transforms and
    `Metadata/model_settings.config` matrices to preserve relative spacing
    without relocating the full plate.
    """
    if abs(scale_percent - 100.0) < 0.001:
        shutil.copy2(source_3mf, output_3mf)
        return

    scale_factor = float(scale_percent) / 100.0
    ET.register_namespace("", CORE_NS)

    with zipfile.ZipFile(source_3mf, "r") as zin, zipfile.ZipFile(
        output_3mf, "w", compression=zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)

            if info.filename.endswith(".model"):
                data = _scale_component_offsets_model_xml(data, scale_factor)
            elif info.filename == MODEL_SETTINGS_PATH:
                data = _scale_component_offsets_model_settings_xml(data, scale_factor)

            zout.writestr(info, data)
