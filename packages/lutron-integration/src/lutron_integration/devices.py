from dataclasses import dataclass
import re
from . import types, connection
import logging

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArraySpec:
    count: int
    base: int  # First component number
    stride: int = 1  # Spacing between component numbers


@dataclass(frozen=True)
class ComponentGroup:
    name: str  # Programmer-friendly name, like "ZONE"
    desc: str  # Base description without number, like "Zone Controller"
    array_spec: ArraySpec | None = None  # Array specification (for array mode)
    numbers: tuple[int, ...] | None = (
        None  # Explicit list of component numbers (for arbitrary mode)
    )

    @property
    def count(self) -> int:
        if self.array_spec is not None:
            return self.array_spec.count
        else:
            assert self.numbers is not None
            return len(self.numbers)

    def __post_init__(self):
        # Validate that exactly one mode is specified
        if (self.array_spec is None) == (self.numbers is None):
            raise ValueError("Must specify either array_spec or numbers but not both")

        if self.numbers is not None and not self.numbers:
            raise ValueError("numbers cannot be an empty list")

        # Initialize per-group cache
        object.__setattr__(self, "_cache", {})

    def lookup_component(self, number: int) -> int | None:
        """Check if this group contains a component number.

        Returns the 1-based index if found, None otherwise.
        """
        if self.numbers is not None:
            # Arbitrary mode
            try:
                return self.numbers.index(number) + 1
            except ValueError:
                return None
        else:
            # Array mode
            assert self.array_spec is not None
            if number >= self.array_spec.base:
                offset = number - self.array_spec.base
                if offset % self.array_spec.stride == 0:
                    index = offset // self.array_spec.stride + 1
                    if 1 <= index <= self.array_spec.count:
                        return index
            return None

    def component_number(self, index: int) -> int | None:
        """Get the component number for a 1-based index.

        Args:
            index: 1-based index into this component group

        Returns:
            Component number if index is valid, None otherwise.
        """
        if index < 1 or index > self.count:
            return None

        if self.numbers is not None:
            # Arbitrary mode
            return self.numbers[index - 1]
        else:
            # Array mode
            assert self.array_spec is not None
            return self.array_spec.base + (index - 1) * self.array_spec.stride


class DeviceClass:
    """Represents a device type with its component groups and individual components."""

    groups: dict[str, ComponentGroup]

    def __init__(self, groups: list[ComponentGroup]):
        """Initialize a device class with component groups and individual components.

        Args:
            groups: List of ComponentGroup instances
            components: Dictionary mapping component names to ComponentGroup instances
        """
        self.groups = {g.name: g for g in groups}

    def lookup_component(self, number: int) -> tuple[ComponentGroup, int] | None:
        """Resolves a component number to a ComponentGroup and index within the group"""

        # TODO: Consider adding a cache

        # Search through component groups to find a match
        for group in self.groups.values():
            index = group.lookup_component(number)
            if index is not None:
                return (group, index)

        return None


FAMILY_TO_CLASS: dict[bytes, DeviceClass] = {}

# Grafik Eye QS Device Definition
GrafikEyeQS = DeviceClass(
    groups=[
        ComponentGroup(
            name="ZONE", desc="Zone Controller", array_spec=ArraySpec(count=24, base=1)
        ),
        ComponentGroup(
            name="SHADE_OPEN", desc="Shade Column Open", numbers=(38, 44, 50)
        ),
        ComponentGroup(
            name="SHADE_PRESET", desc="Shade Column Preset", numbers=(39, 45, 51)
        ),
        ComponentGroup(
            name="SHADE_CLOSE", desc="Shade Column Close", numbers=(40, 46, 56)
        ),
        ComponentGroup(
            name="SHADE_LOWER", desc="Shade Column Lower", numbers=(41, 52, 57)
        ),
        ComponentGroup(
            name="SHADE_RAISE", desc="Shade Column Raise", numbers=(47, 53, 58)
        ),
        ComponentGroup(
            name="SCENE_BUTTON", desc="Scene Button", numbers=(70, 71, 76, 77)
        ),
        ComponentGroup(name="SCENE_OFF_BUTTON", desc="Scene Off Button", numbers=(83,)),
        ComponentGroup(
            name="SCENE_CONTROLLER", desc="Scene Controller", numbers=(141,)
        ),
        ComponentGroup(name="LOCAL_CCI", desc="Local CCI", numbers=(163,)),
        ComponentGroup(
            name="TIMECLOCK_CONTROLLER", desc="Timeclock Controller", numbers=(166,)
        ),
        # The LEDs are not available in QS Standalone
        ComponentGroup(
            name="SCENE_LED",
            desc="Scene LED",
            array_spec=ArraySpec(count=4, base=201, stride=9),
        ),
        ComponentGroup(name="SCENE_OFF_LED", desc="Scene Off LED", numbers=(237,)),
        ComponentGroup(
            name="SHADE_OPEN_LED",
            desc="Shade Column Open LED",
            array_spec=ArraySpec(count=3, base=174, stride=9),
        ),
        ComponentGroup(
            name="SHADE_PRESET_LED",
            desc="Shade Column Preset LED",
            array_spec=ArraySpec(count=3, base=175, stride=9),
        ),
        ComponentGroup(
            name="SHADE_CLOSE_LED",
            desc="Shade Column Close LED",
            array_spec=ArraySpec(count=3, base=211, stride=9),
        ),
        ComponentGroup(
            name="WIRELESS_OCC_SENSOR",
            desc="Wireless Occupancy Sensor",
            array_spec=ArraySpec(count=30, base=500),
        ),
        ComponentGroup(
            name="ECOSYSTEM_OCC_SENSOR",
            desc="EcoSystem Ballast Occupancy Sensor",
            array_spec=ArraySpec(count=64, base=700),
        ),
        # These components are not documented.
        # TODO: Confirm the behavior of the zone buttons on a 16-zone unit
        ComponentGroup(name="MASTER_RAISE", desc="Master Raise Button", numbers=(74,)),
        ComponentGroup(name="MASTER_LOWER", desc="Master Lower Button", numbers=(75,)),
        ComponentGroup(
            name="ZONE_RAISE",
            desc="Zone Raise Button",
            array_spec=ArraySpec(count=8, base=36, stride=6),
        ),
        ComponentGroup(
            name="ZONE_LOWER",
            desc="Zone Lower Button",
            array_spec=ArraySpec(count=8, base=37, stride=6),
        ),
        ComponentGroup(name="TIMECLOCK_BUTTON", desc="Timeclock Button", numbers=(68,)),
        ComponentGroup(name="OK_BUTTON", desc="OK Button", numbers=(69,)),
        ComponentGroup(
            name="SWITCH_GROUP_BUTTON", desc="Swich Group Button", numbers=(80,)
        ),
    ]
)

Shade = DeviceClass(
    groups=[
        # Yes, Lutron really did not document any components!
        # Shades accept a target position as "light level" (action 14)
        # on component 0 and report their position via this action as well.
        ComponentGroup(
            name="SHADE",
            desc="Shade Position",
            numbers=(0,),
        ),
    ]
)

Keypad = DeviceClass(
    groups=[
        # Buttons 1-7: most keypads have these, even if they claim to have fewer
        # buttons.  They might be hiding under the cover plate.
        ComponentGroup(
            name="BUTTON", desc="Button", array_spec=ArraySpec(count=7, base=1)
        ),
        # The top raise/lower buttons only sometimes exist.  The bottom raise/lower
        # buttons are very common.  It's a bit unclear what happens if they are
        # physically absent (e.g. on a "7-button" Architrave keypad, not all
        # programming features are present because the bottom raise/lower buttons
        # don't exist.)
        ComponentGroup(name="BUTTON_TOP_LOWER", desc="Button Top Lower", numbers=(16,)),
        ComponentGroup(name="BUTTON_TOP_RAISE", desc="Button Top Raise", numbers=(17,)),
        ComponentGroup(
            name="BUTTON_BOTTOM_LOWER", desc="Button Top Lower", numbers=(18,)
        ),
        ComponentGroup(
            name="BUTTON_BOTTOM_RAISE", desc="Button Top Raise", numbers=(19,)
        ),
    ]
)

# TODO: This isn't great: a shade power supply is 'SHADES(3)' but is not a shade
FAMILY_TO_CLASS[b"KEYPAD(1)"] = Keypad
FAMILY_TO_CLASS[b"GRAFIK_EYE(2)"] = GrafikEyeQS
FAMILY_TO_CLASS[b"SHADES(3)"] = Shade


def action_to_friendly_str(action: int):
    try:
        return types.DeviceAction(action).name
    except ValueError:
        return str(action)


@dataclass
class DeviceUpdateValues:
    component: int
    action: types.DeviceAction
    params: tuple[bytes]


@dataclass
class DeviceUpdate:
    """Represents a parsed device update message."""

    serial_number: types.SerialNumber
    component: int
    action: types.DeviceAction
    value: tuple[bytes, ...]


_DEVICE_UPDATE_RE = re.compile(rb"~DEVICE,([^,]+),(\d+),(\d+)(?:,([^\r]*))?\r\n", re.S)


def decode_device_update(
    message: bytes, iidmap: types.IntegrationIDMap
) -> DeviceUpdate | None:
    """Parse a ~DEVICE message into a DeviceUpdate.

    Args:
        message: Raw ~DEVICE message bytes
        universe: LutronUniverse for resolving device identifiers

    Returns:
        DeviceUpdate if message was parsed successfully, None otherwise
    """

    # ~DEVICE,<identifier>,<component>,<action>[,<params>]\r\n
    match = _DEVICE_UPDATE_RE.fullmatch(message)
    if not match:
        _LOGGER.debug(f"Failed to parse device message: {message!r}")
        return None

    device_identifier = match[1]
    component = int(match[2])
    action_int = int(match[3])
    value = tuple(match[4].split(b",")) if match[4] else ()

    # Resolve device identifier to serial number
    try:
        sn = types.SerialNumber(device_identifier)
    except ValueError:
        # Not a serial number, try integration ID
        if device_identifier in iidmap.device_ids:
            sn = iidmap.device_ids[device_identifier]
        else:
            _LOGGER.debug("Unknown device identifier: %s", device_identifier)
            return None

    try:
        action = types.DeviceAction(action_int)
    except ValueError:
        _LOGGER.debug(f"Unknown action {action_int} in update {message!r}")
        return None

    return DeviceUpdate(
        serial_number=sn, component=component, action=action, value=value
    )


async def probe_device(
    conn: connection.LutronConnection,
    iidmap: types.IntegrationIDMap,
    dev_id: types.SerialNumber | bytes,
) -> list[DeviceUpdate]:
    result: list[DeviceUpdate] = []
    _, updates = await conn.probe_device(dev_id)

    for update in updates:
        decoded = decode_device_update(update, iidmap)
        if decoded is not None:
            result.append(decoded)

    return result


_IIDLINE_RE = re.compile(
    b"~INTEGRATIONID,([^,]+),(DEVICE|OUTPUT),([0-9A-Fa-fx]+)(?:,([0-9]+))?", re.S
)


async def enumerate_iids(conn: connection.LutronConnection) -> types.IntegrationIDMap:
    iidmap = types.IntegrationIDMap()

    integration_ids = await conn.raw_query(b"?INTEGRATIONID,3")
    iidlines = integration_ids.split(b"\r\n")

    if not iidlines or iidlines[-1] != b"":
        raise types.ParseError("~INTEGRATIONIDS,3 list does not split correctly")
    iidlines = iidlines[:-1]

    for line in iidlines:
        m = _IIDLINE_RE.fullmatch(line)
        if not m:
            raise types.ParseError(f"Integration id line {line!r} does not parse")
        name = m[1]
        if name == b"(Not Set)":
            # If we ever support a dialect that does not support ?DETAILS, then we could
            # use the (Not Set) lines as a way to discover devices without integration IDs.
            # This is not necessary for QS Standalone.
            continue
        sn = types.SerialNumber(m[3])
        if m[2] == b"DEVICE":
            iidmap.device_ids[name] = sn
        else:
            iidmap.output_ids[name] = (sn, int(m[4]))

    return iidmap
