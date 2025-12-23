from dataclasses import dataclass, field
import re
from enum import Enum, unique

_SN_RE = re.compile(b"(?:0x)?([0-9A-Fa-f]{0,8})", re.S)


# A serial number as reported by the integration access point
# is an optional 0x followed by
# 8 hexadecimal digits, with inconsistent case.  The NWK accepts a serial
# number with up to two 0x prefixes followed by any number (maybe up to
# some limit) of zeros followed by case-insensitive hex digits.
#
# Some but not all commands will accept an integration id in place of
# a serial number.  The NWK can get extremely confused if there is an
# integration id that is also a well-formed serial number.
#
# (All of this comes from testing a QSE-CI-NWK-E, but I expect
#  it to be compatible with other integration access points
#  as well.)
#
# This class represents a canonicalized serial number.  It's hashable.
@dataclass(order=False, eq=True, frozen=True)
class SerialNumber:
    sn: bytes

    def __init__(self, sn: bytes):
        m = _SN_RE.fullmatch(sn)
        if not m:
            raise ValueError(f"Malformed serial number {sn!r}")
        sn = m[1]
        object.__setattr__(self, "sn", b"0" * (8 - len(sn)) + sn.upper())

    def __repr__(self):
        return f"SerialNumber({self.sn!r})"

    def __str__(self):
        return self.sn.decode()


# These are DEVICE actions.  OUTPUT actions are different.
@unique
class DeviceAction(Enum):
    ENABLE = 1
    DISABLE = 2
    PRESS_CLOSE_UNOCC = 3  # Press button or close shades or room unoccupied
    RELEASE_OPEN_OCC = 4  # Release button or open shades or room occupied
    HOLD = 5
    DOUBLE_TAP = 6
    CURRENT_SCENE = 7
    LED_STATE = 9
    SCENE_SAVE = 12
    LIGHT_LEVEL = 14
    ZONE_LOCK = 15
    SCENE_LOCK = 16
    SEQUENCE_STATE = 17
    START_RAISING = 18
    START_LOWERING = 19
    STOP_RAISING_LOWERING = 20
    HOLD_RELEASE = 32  # for keypads -- I have no idea what it does
    TIMECLOCK_STATE = 34  # 0 = disabled, 1 = enabled

    # 21 is a mysterious property of the SHADE component of shades.
    # It seems to have the value 0 most of the time but has other values when the shade
    # is moving.
    MOTOR_MYSTERY = 21


@unique
class OutputAction(Enum):
    LIGHT_LEVEL = 1
    START_RAISING = 2
    START_LOWERING = 3
    STOP_RAISING_LOWERING = 4
    START_FLASHING = 5
    PULSE_TIME = 6
    TILT_LEVEL = 9
    LIFT_TILT_LEVEL = 10
    START_RAISING_TILT = 11
    START_LOWERING_TILT = 12
    STOP_RAISING_LOWERING_TILT = 13
    START_RAISING_LIFT = 14
    START_LOWERING_LIFT = 15
    STOP_RAISING_LOWERING_LIFT = 16
    DMX_COLOR_LEVEL = 17


@dataclass
class IntegrationIDMap:
    # Maps output integration ids to the device sn and output/zone number
    output_ids: dict[bytes, tuple[SerialNumber, int]] = field(default_factory=dict)

    # Maps device integration ids to the device sn
    device_ids: dict[bytes, SerialNumber] = field(default_factory=dict)

    # We don't bother storing the reverse mapping anywhere -- we need
    # to be able to control outputs that don't have integration IDs,
    # and there appears to be no benefit to ever sending an #OUTPUT command.


class ParseError(Exception):
    """Exception raised when a message doesn't parse correctly."""

    def __init__(self, message: str) -> None:
        super().__init__(str)
