"""Printer profiles, filament presets, and build volume definitions."""

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class PrinterProfile:
    name: str
    build_volume_x: float  # mm
    build_volume_y: float  # mm
    build_volume_z: float  # mm


@dataclass
class FilamentPreset:
    name: str
    material: str
    nozzle_temp: int  # °C
    bed_temp: int  # °C
    print_speed: int  # mm/s


PRINTER_PROFILES: Dict[str, PrinterProfile] = {
    "snapmaker_u1": PrinterProfile(
        name="Snapmaker U1",
        build_volume_x=300.0,
        build_volume_y=250.0,
        build_volume_z=235.0,
    ),
}

# Default filament presets for common materials
DEFAULT_FILAMENTS: List[FilamentPreset] = [
    FilamentPreset(
        name="PLA Standard",
        material="PLA",
        nozzle_temp=200,
        bed_temp=60,
        print_speed=60,
    ),
    FilamentPreset(
        name="PETG Standard",
        material="PETG",
        nozzle_temp=235,
        bed_temp=80,
        print_speed=50,
    ),
    FilamentPreset(
        name="ABS Standard",
        material="ABS",
        nozzle_temp=240,
        bed_temp=100,
        print_speed=50,
    ),
    FilamentPreset(
        name="TPU Flexible",
        material="TPU",
        nozzle_temp=220,
        bed_temp=50,
        print_speed=30,
    ),
]


def get_printer_profile(profile_name: str = "snapmaker_u1") -> PrinterProfile:
    if profile_name not in PRINTER_PROFILES:
        raise ValueError(f"Unknown printer profile: {profile_name}")
    return PRINTER_PROFILES[profile_name]
