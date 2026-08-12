"""Windows Energy Meter Interface (EMI) backend.

Uses the built-in Windows EMI device interface (GUID_DEVICE_ENERGY_METER)
to read RAPL / AMD energy counters without a third-party kernel driver.
Typically available on Windows 11 bare metal; no admin rights required.

Reference: https://learn.microsoft.com/en-us/windows-hardware/drivers/powermeter/energy-meter-interface
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from power_monitor.backends.base import Domain
from power_monitor.schema import map_domain_name_to_key

# CTL_CODE(FILE_DEVICE_UNKNOWN, fn, METHOD_BUFFERED, FILE_READ_ACCESS)
IOCTL_EMI_GET_VERSION = 0x224000
IOCTL_EMI_GET_METADATA_SIZE = 0x224004
IOCTL_EMI_GET_METADATA = 0x224008
IOCTL_EMI_GET_MEASUREMENT = 0x22400C

EMI_VERSION_V1 = 1
EMI_VERSION_V2 = 2
EMI_MEASUREMENT_SIZE = 16  # ULONGLONG AbsoluteEnergy + ULONGLONG AbsoluteTime

GUID_DEVICE_ENERGY_METER = "{45BD8344-7ED6-49CF-A440-C276C933B053}"

# AbsoluteEnergy is in picowatt-hours
PWH_TO_JOULES = 3.6e-9

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_CR_SUCCESS = 0
_CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0

_IS_WINDOWS = sys.platform.startswith("win")

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.DeviceIoControl.restype = wintypes.BOOL
    _kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def pwh_to_joules(pwh: int) -> float:
    """Convert picowatt-hours to joules."""
    return pwh * PWH_TO_JOULES


def list_emi_device_paths() -> list[str]:
    """Enumerate present EMI device interface paths."""
    if not _IS_WINDOWS:
        return []
    guid = _GUID()
    if _ole32.CLSIDFromString(GUID_DEVICE_ENERGY_METER, ctypes.byref(guid)) != 0:
        return []
    size = wintypes.ULONG(0)
    cr = _cfgmgr32.CM_Get_Device_Interface_List_SizeW(
        ctypes.byref(size),
        ctypes.byref(guid),
        None,
        _CM_GET_DEVICE_INTERFACE_LIST_PRESENT,
    )
    if cr != _CR_SUCCESS or size.value <= 1:
        return []
    buffer = ctypes.create_unicode_buffer(size.value)
    cr = _cfgmgr32.CM_Get_Device_Interface_ListW(
        ctypes.byref(guid),
        None,
        buffer,
        size,
        _CM_GET_DEVICE_INTERFACE_LIST_PRESENT,
    )
    if cr != _CR_SUCCESS:
        return []
    paths: list[str] = []
    current: list[str] = []
    for char in buffer[: size.value]:
        if char == "\x00":
            if current:
                paths.append("".join(current))
                current = []
        else:
            current.append(char)
    return paths


class _EmiDeviceHandle:
    """Context manager around a CreateFile handle on an EMI device."""

    def __init__(self, device_path: str):
        self._device_path = device_path
        self._handle = None

    def __enter__(self):
        handle = _kernel32.CreateFileW(
            self._device_path,
            _GENERIC_READ,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), self._device_path)
        self._handle = handle
        return self

    def __exit__(self, *exc_info):
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def ioctl(self, code: int, output_size: int) -> bytes:
        output = ctypes.create_string_buffer(output_size)
        returned = wintypes.DWORD(0)
        ok = _kernel32.DeviceIoControl(
            self._handle,
            code,
            None,
            0,
            output,
            output_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), self._device_path)
        return output.raw[: returned.value]


def _decode_wchars(raw: bytes) -> str:
    return raw.decode("utf-16-le", errors="replace").split("\x00")[0]


def parse_channel_names(version: int, metadata: bytes) -> list[str]:
    """Extract channel names from an EMI_METADATA_V1/V2 buffer."""
    if version == EMI_VERSION_V2:
        (channel_count,) = struct.unpack_from("<H", metadata, 66)
        names = []
        offset = 68
        for _ in range(channel_count):
            (name_size,) = struct.unpack_from("<H", metadata, offset + 4)
            raw = metadata[offset + 6 : offset + 6 + name_size]
            names.append(_decode_wchars(raw))
            offset += 6 + name_size
        return names
    if version == EMI_VERSION_V1:
        (name_size,) = struct.unpack_from("<H", metadata, 70)
        raw = metadata[72 : 72 + name_size]
        return [_decode_wchars(raw) or "EMI"]
    raise ValueError(f"Unsupported EMI version: {version}")


def parse_measurements(raw: bytes) -> list[tuple[int, int]]:
    """Parse EMI_CHANNEL_MEASUREMENT_DATA into (energy_pwh, time_100ns) tuples."""
    measurements = []
    for offset in range(0, len(raw) - EMI_MEASUREMENT_SIZE + 1, EMI_MEASUREMENT_SIZE):
        measurements.append(struct.unpack_from("<QQ", raw, offset))
    return measurements


def map_emi_channel_to_key(name: str) -> Optional[str]:
    """Map an EMI channel name to a schema domain key.

    Intel RAPL-style: RAPL_Package0_PKG / _PP0 / _PP1 / _DRAM
    AMD-style (Chromium): Current Socket Energy, Apu Energy, VDDCR_VDD, VDDCR_SOC
    """
    lower = name.lower().strip()

    # Explicit AMD names first
    if lower in ("current socket energy", "apu energy", "vddcr_vdd energy"):
        return "package"
    if lower == "vddcr_soc energy":
        return "uncore"

    # Skip per-core RAPL channels (would over-count vs package)
    if "_core" in lower and "package" in lower and "pkg" not in lower:
        return None
    if lower.endswith("_core") or "_core" in lower and "pp0" not in lower:
        # e.g. RAPL_Package0_Core3_CORE
        if "pkg" not in lower and "pp0" not in lower and "pp1" not in lower:
            return None

    return map_domain_name_to_key(name)


# Preference order when multiple channels map to the same key
_PACKAGE_PREFERENCE = (
    "current socket energy",
    "apu energy",
    "rapl_package0_pkg",
    "vddcr_vdd energy",
)


def _package_rank(name: str) -> int:
    lower = name.lower()
    for i, pref in enumerate(_PACKAGE_PREFERENCE):
        if pref in lower or lower == pref:
            return i
    if "pkg" in lower:
        return 10
    return 100


@dataclass
class EmiChannelInfo:
    device_path: str
    channel_index: int
    name: str
    key: str


def probe_emi_channels() -> list[dict]:
    """Return a list of {device, channel, name, key} for all readable EMI channels.

    Safe to call for diagnostics; returns [] on non-Windows or when EMI is absent.
    """
    if not _IS_WINDOWS:
        return []
    results = []
    for device_path in list_emi_device_paths():
        try:
            with _EmiDeviceHandle(device_path) as device:
                (version,) = struct.unpack_from("<H", device.ioctl(IOCTL_EMI_GET_VERSION, 2))
                (metadata_size,) = struct.unpack_from(
                    "<L", device.ioctl(IOCTL_EMI_GET_METADATA_SIZE, 4)
                )
                metadata = device.ioctl(IOCTL_EMI_GET_METADATA, metadata_size)
                names = parse_channel_names(version, metadata)
        except (OSError, ValueError, struct.error):
            continue
        for index, name in enumerate(names):
            results.append({
                "device": device_path,
                "channel": index,
                "name": name,
                "key": map_emi_channel_to_key(name),
            })
    return results


def _enumerate_channels() -> list[EmiChannelInfo]:
    channels: list[EmiChannelInfo] = []
    for device_path in list_emi_device_paths():
        try:
            with _EmiDeviceHandle(device_path) as device:
                (version,) = struct.unpack_from("<H", device.ioctl(IOCTL_EMI_GET_VERSION, 2))
                (metadata_size,) = struct.unpack_from(
                    "<L", device.ioctl(IOCTL_EMI_GET_METADATA_SIZE, 4)
                )
                metadata = device.ioctl(IOCTL_EMI_GET_METADATA, metadata_size)
                names = parse_channel_names(version, metadata)
        except (OSError, ValueError, struct.error):
            continue
        for index, name in enumerate(names):
            key = map_emi_channel_to_key(name)
            if key is None:
                continue
            channels.append(
                EmiChannelInfo(
                    device_path=device_path,
                    channel_index=index,
                    name=name,
                    key=key,
                )
            )
    return channels


def _select_channels(channels: list[EmiChannelInfo]) -> list[EmiChannelInfo]:
    """Pick one channel per schema key; prefer package over core subdomains."""
    by_key: dict[str, list[EmiChannelInfo]] = {}
    for ch in channels:
        by_key.setdefault(ch.key, []).append(ch)

    selected: list[EmiChannelInfo] = []
    for key, group in by_key.items():
        if key == "package":
            group = sorted(group, key=lambda c: _package_rank(c.name))
            selected.append(group[0])
        else:
            # First matching channel for cores/uncore/dram/psys
            selected.append(group[0])
    return selected


def _make_reader(device_path: str, channel_index: int, channel_count: int) -> Callable[[], float]:
    def read_joules(
        path: str = device_path,
        idx: int = channel_index,
        count: int = channel_count,
    ) -> float:
        with _EmiDeviceHandle(path) as device:
            raw = device.ioctl(IOCTL_EMI_GET_MEASUREMENT, EMI_MEASUREMENT_SIZE * count)
        measurements = parse_measurements(raw)
        if idx >= len(measurements):
            raise OSError(f"EMI channel index {idx} out of range")
        energy_pwh, _time = measurements[idx]
        return pwh_to_joules(energy_pwh)

    return read_joules


class WindowsEmiBackend:
    """Read CPU energy counters via the Windows Energy Meter Interface."""

    name = "windows_emi"

    def requires_elevated(self) -> bool:
        return False

    def discover(self) -> list[Domain]:
        if not _IS_WINDOWS:
            return []

        all_channels = _enumerate_channels()
        if not all_channels:
            return []

        selected = _select_channels(all_channels)

        # Need channel count per device for measurement buffer sizing
        channel_counts: dict[str, int] = {}
        for ch in all_channels:
            channel_counts[ch.device_path] = max(
                channel_counts.get(ch.device_path, 0), ch.channel_index + 1
            )
        # Also count unmapped channels on same device
        for device_path in {c.device_path for c in all_channels}:
            try:
                with _EmiDeviceHandle(device_path) as device:
                    (version,) = struct.unpack_from(
                        "<H", device.ioctl(IOCTL_EMI_GET_VERSION, 2)
                    )
                    (metadata_size,) = struct.unpack_from(
                        "<L", device.ioctl(IOCTL_EMI_GET_METADATA_SIZE, 4)
                    )
                    metadata = device.ioctl(IOCTL_EMI_GET_METADATA, metadata_size)
                    names = parse_channel_names(version, metadata)
                    channel_counts[device_path] = len(names)
            except (OSError, ValueError, struct.error):
                pass

        domains: list[Domain] = []
        for ch in selected:
            count = channel_counts.get(ch.device_path, ch.channel_index + 1)
            domains.append(
                Domain(
                    name=ch.name,
                    key=ch.key,
                    read_joules=_make_reader(ch.device_path, ch.channel_index, count),
                    max_joules=None,  # 64-bit cumulative counters; wrap is rare
                )
            )
        return domains
