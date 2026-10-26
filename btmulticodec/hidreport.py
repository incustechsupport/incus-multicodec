"""HID report descriptor parsing and input-report decoding for mice and keyboards.

Only what pointing devices and keyboards need: relative X/Y, wheels, buttons, keyboard keys and
consumer (media) keys. Absolute pointers, LEDs and feature reports are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PAGE_GENERIC_DESKTOP = 0x01
PAGE_KEYBOARD = 0x07
PAGE_BUTTON = 0x09
PAGE_CONSUMER = 0x0C

USAGE_X, USAGE_Y, USAGE_WHEEL = 0x30, 0x31, 0x38
USAGE_AC_PAN = 0x238  # consumer page: horizontal scroll
KEY_ERROR_ROLLOVER = 0x01

# Boot-protocol descriptors, for devices whose real descriptor can't be read
BOOT_MOUSE = bytes.fromhex(
    "05010902a1010901a1000509190129051500250195057501810295017503810305010930093109381581257f750895038106c0c0"
)
BOOT_KEYBOARD = bytes.fromhex(
    "05010906a101050719e029e71500250175019508810295017508810195067508150025650507190029658100c0"
)


@dataclass
class Field:
    report_id: int
    bit_offset: int
    size: int
    count: int
    page: int
    usages: list[int]  # one per element for variable fields; [min..max] range for arrays
    logical_min: int
    logical_max: int
    is_array: bool
    is_relative: bool


@dataclass
class InputState:
    buttons: set[int] = field(default_factory=set)  # 1 = left, 2 = right, 3 = middle, 4/5 = back/forward
    dx: int = 0
    dy: int = 0
    wheel: int = 0
    hwheel: int = 0
    keys: set[int] = field(default_factory=set)  # keyboard-page usages, including modifiers 0xE0-0xE7
    consumer: set[int] = field(default_factory=set)
    has_pointer: bool = False
    has_keys: bool = False
    has_consumer: bool = False


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if bits and value & (1 << (bits - 1)) else value


class ReportDescriptor:
    def __init__(self, data: bytes):
        self.fields: list[Field] = []
        self.uses_report_ids = False
        self._parse(data)
        self.report_ids = sorted({f.report_id for f in self.fields})

    def _parse(self, data: bytes) -> None:
        g = {"page": 0, "lmin": 0, "lmax": 0, "size": 0, "count": 0, "id": 0}
        stack = []
        usages: list[int] = []
        umin = umax = None
        offsets: dict[int, int] = {}
        i = 0
        while i < len(data):
            prefix = data[i]
            if prefix == 0xFE:  # long item
                i += 3 + data[i + 1]
                continue
            size = (0, 1, 2, 4)[prefix & 3]
            kind, tag = (prefix >> 2) & 3, prefix >> 4
            raw = int.from_bytes(data[i + 1:i + 1 + size], "little")
            i += 1 + size
            if kind == 1:  # global
                if tag == 0:
                    g["page"] = raw
                elif tag == 1:
                    g["lmin"] = _signed(raw, size * 8)
                elif tag == 2:
                    g["lmax"] = _signed(raw, size * 8) if g["lmin"] < 0 else raw
                elif tag == 7:
                    g["size"] = raw
                elif tag == 8:
                    g["id"] = raw
                    self.uses_report_ids = True
                elif tag == 9:
                    g["count"] = raw
                elif tag == 10:
                    stack.append(dict(g))
                elif tag == 11 and stack:
                    g = stack.pop()
            elif kind == 2:  # local; 4-byte usages carry their own page in the high word
                value = raw if size == 4 else (g["page"] << 16) | raw
                if tag == 0:
                    usages.append(value)
                elif tag == 1:
                    umin = value
                elif tag == 2:
                    umax = value
            elif kind == 0:  # main
                if tag == 8:  # Input
                    self._add_input(g, raw, usages, umin, umax, offsets)
                elif tag == 9 or tag == 11:  # Output / Feature: no input bits consumed
                    pass
                usages, umin, umax = [], None, None

    def _add_input(self, g, flags, usages, umin, umax, offsets) -> None:
        rid = g["id"]
        offset = offsets.get(rid, 0)
        bits = g["size"] * g["count"]
        offsets[rid] = offset + bits
        if flags & 0x01 or not g["count"]:  # constant padding
            return
        if umin is not None and umax is not None:
            span = list(range(umin, umax + 1))
        else:
            span = usages
        if not span:
            return
        page = span[0] >> 16 or g["page"]
        is_array = not flags & 0x02
        if is_array:
            usage_list = span
        else:
            usage_list = [(span[min(k, len(span) - 1)]) & 0xFFFF for k in range(g["count"])]
        self.fields.append(Field(rid, offset, g["size"], g["count"], page,
                                 [u & 0xFFFF for u in usage_list], g["lmin"], g["lmax"],
                                 is_array, bool(flags & 0x04)))

    def decode(self, report: bytes, report_id: int | None = None) -> InputState | None:
        """Decode one input report. `report` includes the report ID byte when report_id is None
        and the descriptor uses IDs; pass report_id when the transport supplies it separately (GATT)."""
        if report_id is None:
            if self.uses_report_ids:
                if not report:
                    return None
                report_id, report = report[0], report[1:]
            else:
                report_id = 0
        fields = [f for f in self.fields if f.report_id == report_id]
        if not fields:
            return None
        value = int.from_bytes(report, "little")
        total = len(report) * 8
        state = InputState()
        for f in fields:
            for k in range(f.count):
                start = f.bit_offset + k * f.size
                if start + f.size > total:
                    break
                raw = (value >> start) & ((1 << f.size) - 1)
                v = _signed(raw, f.size) if f.logical_min < 0 else raw
                if f.is_array:
                    if v < f.logical_min or v > f.logical_max:
                        continue
                    index = v - f.logical_min
                    if index >= len(f.usages):
                        continue
                    self._apply(state, f.page, f.usages[index], 1, f)
                else:
                    self._apply(state, f.page, f.usages[k], v, f)
        if KEY_ERROR_ROLLOVER in state.keys:
            return None  # too many keys held; the keyboard says this report is meaningless
        return state

    @staticmethod
    def _apply(state: InputState, page: int, usage: int, v: int, f: Field) -> None:
        if page == PAGE_GENERIC_DESKTOP and f.is_relative or (page == PAGE_GENERIC_DESKTOP and usage == USAGE_WHEEL):
            state.has_pointer = True
            if usage == USAGE_X:
                state.dx += v
            elif usage == USAGE_Y:
                state.dy += v
            elif usage == USAGE_WHEEL:
                state.wheel += v
        elif page == PAGE_BUTTON:
            state.has_pointer = True
            if v:
                state.buttons.add(usage)
        elif page == PAGE_KEYBOARD:
            state.has_keys = True
            if v and usage:
                state.keys.add(usage)
        elif page == PAGE_CONSUMER:
            if usage == USAGE_AC_PAN:
                state.has_pointer = True
                state.hwheel += v
            else:
                state.has_consumer = True
                if v and usage:
                    state.consumer.add(usage)
