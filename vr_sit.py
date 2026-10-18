"""VRSit - sit in chairs in No Man's Sky VR (NMS.py / pyMHF mod).

What it does, and nothing else:
  1. Patches out the "no chairs in VR" check so chairs offer their Sit prompt in VR.
  2. Reads the game's own sitting state. While your character sits and you sit down physically too
     (head >0.3 m below your standing height, then still for 2 s), it triggers the game's recentre.
  3. While the game recentres seated, feeds it the headset/hand poses raised by 0.5 m for half a
     second, so the view ends up at seat height instead of standing height.
  4. Lowers the visible VR body by 0.4 m while seated: in VR the game draws the seated body that much
     too high, floating above the seat. The correction blends in and out as you sit and stand.
  5. Moves your character under your head as you walk around the play space. The game keeps the body
     at the play-space centre and only lets the camera wander; VRSit walks the player (the real one, so
     other players see it) after your head with the game's own locomotion (FOLLOW_WALK; teleporting it
     by the head offset every frame is the fallback) and shifts the game's own head-reset matrix by the
     same amount, so the view never moves and the HUD comes along.
  6. Lifts the VR play-area limits (MaximumHorizontalOffsetVR, the crouch / head-height limits) so the
     body can follow you; the game's "outside the play area" warning no longer shows.
  Standing up physically while your character stays seated only pauses the seat-height recentre.

Install and launch: see the README in the VRSit release package. Writes a log to vr_sit.log next to this file.
The SteamVR hooks (pose feed + ResetZeroPose) can only be resolved once the game has
initialised OpenVR, so on a cold start the mod reloads itself once when VR comes up.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import re
import struct
import sys
import time
from dataclasses import dataclass

import pefile
import pymhf.core._internal as _internal
from pymhf import FUNCDEF, Mod, ModState
from pymhf.core.hooking import manual_hook
from pymhf.core.memutils import find_pattern_in_binary, get_addressof
from pymhf.core.mod_loader import mod_manager
from pymhf.gui.decorators import BOOLEAN, FLOAT, gui_button, gui_group

import nmspy.data.basic_types as basic
import nmspy.data.types as nms
from nmspy.engine import GetNodeAbsoluteTransMatrix

logger = logging.getLogger("VRSit")

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vr_sit.log")
for _h in [h for h in logging.getLogger().handlers if getattr(h, "name", "") == "vr_sit_file"]:
    logging.getLogger().removeHandler(_h)  # older versions put the file handler on the root logger
    _h.close()
if not any(getattr(h, "name", "") == "vr_sit_file" for h in logger.handlers):
    try:
        _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        _fh.name = "vr_sit_file"
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        logger.addHandler(_fh)
    except Exception:
        pass

# ---------------------------------------------------------------------------- tunables ----
# The ones listed in SETTINGS below can be changed in the pyMHF window and are saved to vr_sit_settings.json.
CHAIRS_IN_VR = True  # patch out the "no chairs in VR" check, so chairs offer their Sit prompt
SEAT_RECENTER = True  # when you sit down physically on an in-game seat, recentre to seat height
SEAT_BODY_FIX = True  # lower the seated VR body, which the game draws floating above the seat
SIT_DROP_M = 0.3  # head this far below its recent standing peak = physically sitting
STAND_DROP_M = 0.2  # head back within this of the standing peak = physically standing again
SIT_STILL_M = 0.08  # after sitting down, the head must stay within this radius...
SIT_RECENTER_DELAY = 2.0  # ...for this many seconds before the automatic recentre
HEAD_PEAK_WINDOW = 30.0  # s; the standing peak is forgotten after this long (only while not seated)
RECENTER_INJECT_M = 0.5  # how far below standing eye height the seated view should end up
RECENTER_INJECT_SECONDS = 0.5  # how long the raised poses are fed after ResetZeroPose
SEAT_BODY_DROP_M = 0.45  # how far the visible VR body is lowered while seated
BODY_BLEND_SECONDS = 0.5  # time constant for blending the body correction in and out
FOLLOW_MOVE = True  # the character walks after you as you move around the play space
FOLLOW_DEADBAND_M = 0.0005  # head offsets (horizontal) smaller than this are left alone (keep tiny: an
#   intermittent step shows as a one-frame hand flicker, a continuous one only as a sub-millimetre lag)
FOLLOW_MAX_SPEED = 3.0  # m/s; the body never chases the head faster than this (tracking glitches)
FOLLOW_WALK = True  # walk the character under your head with the game's own locomotion (else teleport it)
WALK_BY_STICK = False  # walk by emulating the Move stick through SteamVR (the old way, with the stick's dead
#   zone) instead of writing the walking force cGcPlayer::CalculatePlayerForce returns; also the fallback
STRIDE_START_M = 0.2  # the body starts a stride once your head is this far from its target spot (the walk
#   animation is made for real strides: tiny steps looked glitchy)...
STRIDE_STOP_M = 0.05  # ...and stops once it has closed to this (hysteresis: no creeping back and forth)
FOLLOW_BEHIND_M = 0.25  # the body aims to stand this far behind your head, not under it: the stride circle is
#   shifted back by this much, so stepping back never brings your head into the body (it clipped into the
#   view); with this >= the start distance the body never ends up in front of your head
FORCE_GAIN = 1.5  # walking force per metre of head offset (1.0 = the full force a pushed stick gives)
FORCE_MIN = 0.15  # smallest force written: no slow creep at the end of a stride (the game counts |force| >= 0.032 as moving)
FORCE_MAX = 0.6  # a brisk walk
FORCE_LEAD_S = 0.15  # damping, as WALK_LEAD_S
WALK_GAIN = 1.0  # stick deflection per metre of head offset (measured: 0.74 stick = 1.8 m/s, 1.0 = 3.7 m/s run)
WALK_MIN_STICK = 0.2  # smallest stick deflection that still moves the character
WALK_MAX_STICK = 0.7  # never more than a brisk walk: the run speed overshoots and oscillates
WALK_LEAD_S = 0.12  # damping: aim this many seconds ahead using the body's current velocity
BODY_VEL_SMOOTH_S = 0.1  # time constant smoothing the measured body velocity for that
WALK_PREDICT = False  # also take the expected walk out of the view before physics (if the camera is
#   computed before the post-physics correction, this removes the one-frame lead)
VIEW_HOLD = True  # anchor the view to a fixed world point and re-solve the head-reset matrix right before
#   the camera reads the body, every frame: body motion never moves the view unless you use the stick
VIEW_HOLD_TELEPORT_M = 1.0  # a body jump bigger than this per frame re-anchors (game teleported you)
VIEW_HOLD_RECENTER_S = 0.5  # release the anchor this long after a recentre
VIEW_HOLD_LAG_FRAMES = 0  # solve against the body position this many camera frames back (no difference felt)
MAX_HEAD_OFFSET_M = 10.0  # GcPlayerGlobals.MaximumHorizontalOffsetVR: the game clamps the view to this radius
#   around the body (0.75 m by default) - with the body following you that only causes jolts, so lift it
WALK_ADAPT_RATE = 0.0  # how quickly the stick-to-direction mapping follows what the body really does (0 = fixed;
#   adapting per frame went haywire because the body's inertia makes its direction lag the command)
# Stick (x, y) -> body-frame direction (right, at) to start from: measured on 180836, stick forward moves the
# body along -at, stick right along +right. The mapping keeps adapting from what the body actually does.
WALK_MAP_DEFAULT = (1.0, 0.0, 0.0, -1.0)
WALK_SETTLE_S = 0.6  # keep correcting the view this long after the last push (the body decelerates)
STICK_DEADZONE = 0.25  # your Move stick counts as used beyond this (a resting stick drifted to 0.18 once)
FOLLOW_YAW = True  # turn the character to where you face
MENU_NO_TURN = True  # ...except while a menu is open that is not a hand menu (its panel would turn away)
DIALOG_NO_TURN = True  # ...or a dialog screen (talking to someone, terminals, missions, trade)
YAW_DEADBAND_DEG = 0.0  # look this far away from the body's forward before it starts turning after you
YAW_ALIGNED_DEG = 0.0  # ...and it keeps turning until it is this close again (0 = body locked to head yaw)
YAW_RATE_DEG = 180.0  # degrees per second the body turns at

# ------------------------------------------------------------------------ settings ----
# Shown in the VRSit tab of the pyMHF window and saved to vr_sit_settings.json next to this file. The file can
# also be edited by hand; it is re-read while the game runs. (group, tunable, type, label, min, max, step)
SETTINGS = (
    ("Chairs", "CHAIRS_IN_VR", bool, "Chairs offer Sit in VR", None, None, None),
    ("Chairs", "SEAT_RECENTER", bool, "Recentre to seat height when you sit down", None, None, None),
    ("Chairs", "SEAT_BODY_FIX", bool, "Lower the floating seated body", None, None, None),
    ("Chairs", "RECENTER_INJECT_M", float, "Seated view: this far below standing height (m)", 0.0, 1.2, 0.05),
    ("Chairs", "SEAT_BODY_DROP_M", float, "Seated body: lowered by (m)", 0.0, 1.0, 0.05),
    ("Chairs", "SIT_DROP_M", float, "Head drop that counts as sitting down (m)", 0.1, 1.0, 0.05),
    ("Chairs", "SIT_RECENTER_DELAY", float, "Sit still this long before the recentre (s)", 0.5, 10.0, 0.5),
    ("Body follow", "FOLLOW_MOVE", bool, "Body walks after you", None, None, None),
    ("Body follow", "FOLLOW_YAW", bool, "Body turns to where you face", None, None, None),
    ("Body follow", "MENU_NO_TURN", bool, "...but not while a menu (not a hand menu) is open", None, None, None),
    ("Body follow", "DIALOG_NO_TURN", bool, "...nor in dialog screens", None, None, None),
    ("Body follow", "VIEW_HOLD", bool, "Keep the view still while the body walks", None, None, None),
    ("Body follow", "WALK_BY_STICK", bool, "Walk by emulating the stick (old way, has a dead zone)", None, None, None),
    ("Walking", "STRIDE_START_M", float, "Stride starts when your head is this far off (m)", 0.02, 1.0, 0.01),
    ("Walking", "STRIDE_STOP_M", float, "Stride ends this close (m)", 0.0, 0.5, 0.01),
    ("Walking", "FOLLOW_BEHIND_M", float, "Body stands this far behind your head (m)", 0.0, 0.6, 0.01),
    ("Walking", "FORCE_MIN", float, "Slowest walking force (0-1)", 0.035, 1.0, 0.01),
    ("Walking", "FORCE_MAX", float, "Fastest walking force (0-1)", 0.05, 1.0, 0.05),
    ("Walking", "FORCE_GAIN", float, "Walking force per metre off", 0.1, 10.0, 0.1),
    ("Walking", "FORCE_LEAD_S", float, "Braking: look ahead (s)", 0.0, 0.5, 0.01),
    ("Turning", "YAW_DEADBAND_DEG", float, "Start turning when you look this far away (deg)", 0.0, 90.0, 1.0),
    ("Turning", "YAW_ALIGNED_DEG", float, "...and keep turning until this close (deg)", 0.0, 90.0, 1.0),
    ("Turning", "YAW_RATE_DEG", float, "Turn speed (deg/s)", 10.0, 720.0, 10.0),
    ("Stick", "STICK_DEADZONE", float, "Your Move stick counts as used beyond (0-1)", 0.05, 0.9, 0.05),
)
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vr_sit_settings.json")
SETTING_DEFAULTS = {name: globals()[name] for _g, name, *_rest in SETTINGS}
SETTING_SPECS = {name: (kind, lo, hi) for _g, name, kind, _label, lo, hi, _step in SETTINGS}


def _coerce_setting(name: str, value):
    kind, lo, hi = SETTING_SPECS[name]
    if kind is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(value)
    return min(hi, max(lo, v))


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def save_settings() -> float:
    """Write the current settings to SETTINGS_FILE; returns its new modification time."""
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({name: globals()[name] for _g, name, *_rest in SETTINGS}, f, indent=2)
            f.write("\n")
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        logger.exception("could not save %s", SETTINGS_FILE)
    return _mtime(SETTINGS_FILE)


def load_settings() -> float:
    """Apply SETTINGS_FILE to the tunables (creating it with the defaults when missing); returns its mtime."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return save_settings()
    except Exception:
        logger.exception("could not read %s; keeping the current settings", SETTINGS_FILE)
        return _mtime(SETTINGS_FILE)
    for name in SETTING_SPECS:
        if name in data:
            try:
                globals()[name] = _coerce_setting(name, data[name])
            except Exception:
                logger.warning("setting %s: ignoring bad value %r", name, data[name])
    return _mtime(SETTINGS_FILE)


_settings_mtime = load_settings()

# >>> nms-address-lookup (shared block: keep identical in every mod; tools/check_addresses.py runs it offline) >>>
# Build-specific addresses are looked up in the running NMS.exe by masked byte patterns, so a game update does not
# need a new mod release. Pattern tokens: "48" a byte, "?" any byte, "[2-8]" 2 to 8 any bytes, "* * * *" a rel32
# field (RIP-relative operand or call target: value = end of its instruction + rel32; several "*" fields in one
# pattern must agree), "# # # #" a u32 field (the value itself, e.g. a struct offset), ">" the end of the
# instruction holding the field when an immediate follows it, "^" the result position of a pattern without a
# field (default: its start), "@text" a rel32 that must point at the bytes "text" and "=NAME" a rel32 that must
# point at the address NAME found earlier in the same table (both pick the right one of look-alike sites).
# A pattern counts when all its matches give one value; the counting patterns must agree, and the value must pass
# its kind's check ("data": inside a writable data section, "code": a function start in executable code, plus an
# optional masked "prologue", "value": inside "range") and the optional live "check" ("ptr", "vft", "vft:<class>",
# "u32<N"; a null pointer passes). Nothing found -> 0 and a warning; the caller switches that feature off. Only on
# the build the "known" values were verified on (180836) is a failed lookup replaced by the known value.
import ctypes as _actypes
import re as _are
import sys as _asys

_ADDR_KNOWN_BUILD = (0x6ABE9229, 0x771D000)  # NMS.exe 180836: PE TimeDateStamp, SizeOfImage
_addr_k32 = _actypes.WinDLL("kernel32") if hasattr(_actypes, "WinDLL") else None
if _addr_k32 is not None:
    _addr_k32.GetCurrentProcess.restype = _actypes.c_void_p
    _addr_k32.ReadProcessMemory.argtypes = [_actypes.c_void_p, _actypes.c_void_p, _actypes.c_void_p,
                                            _actypes.c_size_t, _actypes.POINTER(_actypes.c_size_t)]


def _addr_peek(addr: int, n: int):
    """n bytes at an absolute address of this process, or None (never faults)."""
    out, got = _actypes.create_string_buffer(n), _actypes.c_size_t(0)
    if addr and _addr_k32.ReadProcessMemory(_addr_k32.GetCurrentProcess(), _actypes.c_void_p(addr), out, n,
                                            _actypes.byref(got)) and got.value == n:
        return out.raw
    return None


class _AddrImage:
    """Sections and bytes of NMS.exe: the running image (base given) or an exe file (offline checks). Code is read
    from the exe file when it is the running build: the same bytes, minus the mods' hooks and patches."""

    def __init__(self, base: int = 0, path: str = ""):
        import pefile
        self.base, self._file = base, None
        if base:
            head = _addr_peek(base, 0x1000)
            if head is None:
                raise RuntimeError("cannot read the PE header of the running image")
            pe = pefile.PE(data=head, fast_load=True)
        else:
            pe = self._file = pefile.PE(path, fast_load=True)
        self.build = (pe.FILE_HEADER.TimeDateStamp, pe.OPTIONAL_HEADER.SizeOfImage)
        self.sections = [(s.Name.rstrip(b"\0").decode(errors="replace"), s.VirtualAddress, s.Misc_VirtualSize,
                          s.Characteristics) for s in pe.sections]
        if base and path:
            try:
                f = pefile.PE(path, fast_load=True)
                if (f.FILE_HEADER.TimeDateStamp, f.OPTIONAL_HEADER.SizeOfImage) == self.build:
                    self._file = f
            except Exception:
                pass
        self._text = None

    def section(self, rva: int):
        for name, va, size, chars in self.sections:
            if va <= rva < va + size:
                return name, chars
        return None, 0

    def read(self, rva: int, n: int):
        if self.section(rva)[0] is None or self.section(rva + n - 1)[0] is None:
            return None
        if self._file is None:
            return _addr_peek(self.base + rva, n)
        b = self._file.get_data(rva, n)
        return b + b"\0" * (n - len(b))  # uninitialised data reads as zeros

    def text(self):
        if self._text is None:
            _, va, size, _ = next(s for s in self.sections if s[0] == ".text")
            self._text = (va, self.read(va, size))
        return self._text


_ADDR_RX = {}


def _addr_compile(sig: str):
    """Pattern -> (regex, [field token per regex group], {field group: group of its instruction end})."""
    if sig in _ADDR_RX:
        return _ADDR_RX[sig]
    toks, rx, fields, ends, k, last = sig.split(), b"", [], {}, 0, 0
    while k < len(toks):
        t = toks[k]
        if t in ("*", "#"):
            if toks[k:k + 4] != [t] * 4:
                raise ValueError(f"'{t}' must come four in a row: {sig}")
            rx += b"(....)"
            fields.append(t)
            last = len(fields)
            k += 4
            continue
        if t[0] in "@=":
            rx += b"(....)"
            fields.append(t)
            last = len(fields)
        elif t in (">", "^"):
            rx += b"()"
            fields.append(t)
            if t == ">":
                ends[last] = len(fields)
        elif t == "?":
            rx += b"."
        elif t.startswith("["):
            lo, hi = t[1:-1].split("-")
            rx += b".{%d,%d}?" % (int(lo), int(hi))
        else:
            rx += _are.escape(bytes([int(t, 16)]))
        k += 1
    _ADDR_RX[sig] = out = (_are.compile(rx, _are.S), fields, ends)
    return out


def _addr_match_value(img, text_rva: int, m, fields, ends, env):
    """The value one match gives, or None (an @text/=NAME check failed, or its fields disagree)."""
    value, pos = None, None
    for g, f in enumerate(fields, 1):
        if f == "^":
            pos = text_rva + m.start(g)
        if f not in ("*", "#") and f[0] not in "@=":
            continue
        raw = int.from_bytes(m.group(g), "little", signed=f != "#")
        v = raw if f == "#" else text_rva + (m.start(ends[g]) if g in ends else m.end(g)) + raw
        if f.startswith("@"):
            want = f[1:].encode()
            if img.read(v, len(want)) != want:
                return None
        elif f[0] == "=":
            if not env.get(f[1:]) or env[f[1:]] != v:
                return None
        elif value is None:
            value = v
        elif value != v:
            return None
    if value is None:
        value = pos if pos is not None else text_rva + m.start()
    return value


def _addr_scan(img, sig: str, env: dict):
    """Set of values the pattern's matches give in .text (cached per build across mods)."""
    cache = getattr(_asys, "_nms_addr_scan_cache", None)
    if cache is None:
        cache = {}
        setattr(_asys, "_nms_addr_scan_cache", cache)
    rx, fields, ends = _addr_compile(sig)
    key = (img.build, sig) + tuple(env.get(f[1:]) for f in fields if f[0] == "=")
    if key not in cache:
        text_rva, text = img.text()
        vals = {_addr_match_value(img, text_rva, m, fields, ends, env) for m in rx.finditer(text)}
        vals.discard(None)
        cache[key] = frozenset(vals)
    return cache[key]


def _addr_live_check(img, check: str, rva: int):
    """None if the live value at NMS+rva passes `check`, else what is wrong."""
    def u64(a):
        b = _addr_peek(a, 8)
        return int.from_bytes(b, "little") if b else None
    slot = img.base + rva
    if check.startswith("u32<"):
        b = _addr_peek(slot, 4)
        v = int.from_bytes(b, "little") if b else None
        return None if v is not None and v < int(check[4:], 0) else f"value {v} not {check[3:]}"
    obj = u64(slot)
    if obj is None:
        return "slot unreadable"
    if not obj:
        return None  # not created yet: nothing to check
    if check == "ptr":
        return None if _addr_peek(obj, 8) is not None else f"points to unreadable {obj:#x}"
    vft = u64(obj)
    if vft is None:
        return f"points to unreadable {obj:#x}"
    name, chars = img.section(vft - img.base)
    if name is None or chars & 0xA0000000:  # executable or writable: not a vftable
        return f"object {obj:#x} has no vftable in the image"
    if check.startswith("vft:"):
        col = u64(vft - 8)
        hdr = _addr_peek(col, 16) if col else None
        td = int.from_bytes(hdr[12:16], "little") if hdr else 0
        want = (".?AV" + check[4:] + "@@\0").encode()
        if not td or _addr_peek(img.base + td + 16, len(want)) != want:
            return f"object {obj:#x} is not a {check[4:]}"
    return None


def _addr_check(img, spec: dict, v: int):
    """None if v is plausible for the spec's kind, else what is wrong."""
    kind = spec["kind"]
    if kind == "value":
        lo, hi = spec["range"]
        return None if lo <= v < hi else f"{v:#x} outside {lo:#x}..{hi:#x}"
    name, chars = img.section(v)
    if kind == "data":
        if name is None or not chars & 0x80000000 or chars & 0x20000000:
            return f"NMS+0x{v:X} is not in a writable data section ({name})"
    elif kind == "code":
        if name is None or not chars & 0x20000000:
            return f"NMS+0x{v:X} is not in executable code ({name})"
        before = img.read(v - 1, 1)
        if v & 0xF and before not in (b"\xcc", b"\xc3"):
            return f"NMS+0x{v:X} is not a function start"
        if spec.get("prologue"):
            rx = _addr_compile(spec["prologue"])[0]
            code = img.read(v, 64)
            if not code or not rx.match(code):
                return f"NMS+0x{v:X} does not start with the expected prologue"
    else:
        return f"unknown kind {kind!r}"
    if spec.get("check") and img.base:
        return _addr_live_check(img, spec["check"], v)
    return None


def _addr_lookup(img, spec: dict, env: dict):
    """(value or 0, problem or None, per-pattern notes)."""
    found, notes = set(), []
    for sig in spec["sigs"]:
        try:
            vals = _addr_scan(img, sig, env)
        except Exception as e:
            vals = frozenset()
            notes.append(f"error {e!r}")
            continue
        notes.append("1" if len(vals) == 1 else f"{len(vals)} values" if vals else "0")
        if len(vals) == 1:
            found |= vals
    if not found:
        return 0, "no pattern matched", notes
    if len(found) > 1:
        return 0, "patterns disagree: " + ", ".join(f"0x{v:X}" for v in sorted(found)), notes
    v = found.pop()
    problem = _addr_check(img, spec, v)
    return (0, problem, notes) if problem else (v, None, notes)


def _addr_resolve(specs: dict, log, img=None) -> dict:
    """{name: RVA ("data"/"code") or value ("value"), 0 when not found} for an ADDRESSES table."""
    out = {}
    try:
        if img is None:
            import pymhf.core._internal as _pi
            img = _AddrImage(base=_pi.BASE_ADDRESS, path=_pi.BINARY_PATH)
    except Exception:
        log.exception("address lookup: cannot read the game image")
        return {name: 0 for name in specs}
    known_build = img.build == _ADDR_KNOWN_BUILD
    for name, spec in specs.items():
        try:
            v, problem, notes = _addr_lookup(img, spec, out)
        except Exception as e:
            v, problem, notes = 0, f"lookup failed: {e!r}", []
        show = (lambda x: f"0x{x:X}") if spec["kind"] == "value" else (lambda x: f"NMS+0x{x:X}")
        if known_build and v != spec["known"]:
            log.warning("address %s: %s (patterns %s) - build 180836, so using its verified value %s", name,
                        problem or f"patterns give {show(v)}", "/".join(notes), show(spec["known"]))
            v = spec["known"]
        elif problem:
            log.warning("address %s not found: %s (patterns %s) - what needs it stays off", name, problem,
                        "/".join(notes))
        else:
            log.info("address %s = %s (patterns %s)", name, show(v), "/".join(notes))
        out[name] = v
    return out
# <<< nms-address-lookup <<<

# Game addresses (see the block above; tools/check_addresses.py verifies them offline). "known": the
# value on build 180836, used only on that build if the patterns fail there.
ADDRESSES = {
    # gGcPlayerGlobals.MaximumHorizontalOffsetVR (float): its only reader, the VR head-offset check
    # (|offset|^2 > max^2), seen through two windows
    "MAX_HORIZONTAL_VR": {"kind": "data", "known": 0x5249330 + 0x1578, "sigs": [
        "0F 58 C8 F3 0F 10 05 * * * * F3 0F 59 C0 C6 87 ? ? ? ? 00 0F 2F C8",
        "F3 0F 10 05 * * * * F3 0F 59 C0 [0-8] 0F 2F C8 0F 47 C6 3A 87",
    ]},
}
_RESOLVED = _addr_resolve(ADDRESSES, logger)

# ------------------------------------------------------------------ game data layout ----
# Verified on build 180836 (offsets come from the 4.13 PDB and were confirmed live, see PLAN.md).
PLAYER_CHARACTER_SIT = 0x5540  # cGcPlayer::mCharacterSit (cGcCharacterSit)
SIT_STATE = 0x68  # cGcCharacterSit::meState: 0 standing, 1 sitting down, 2 seated, 3 standing up
THIRD_PERSON_MESH_NODE = 0x38  # cGcPlayerThirdPerson::mThirdPersonMeshNode - the visible body
SITTING, SEATED = 1, 2
# gGcPlayerGlobals.MaximumHorizontalOffsetVR (float, RVA; gGcPlayerGlobals + 0x1578 on 180836), 0 = not found
MAX_HORIZONTAL_VR = _RESOLVED["MAX_HORIZONTAL_VR"]
PLAYER_CONTROLLER = 0x160  # cGcPlayer::mpPhysicsController (cTkHavokCharacterController*)
PLAYER_VEHICLE = 0x168  # cGcPlayer::mVehicle (cTkAttachmentPtr): non-null in a ship, exocraft or mech
PLAYER_GRAPHICS_MATRIX = 0x310  # cGcPlayer::mGraphicsMatrix (cTkPhysRelMat34: right, up, at, pos local, pos offset)
PLAYER_POSITION = 0x360  # cGcPlayer::mPosition (cTkVector3, world) - the player's root/head position
PLAYER_LAST_POSITION = 0x33B0  # cGcPlayer::mLastPosition (cTkPhysRelVec3)
PLAYER_SET_POSITION = 0x33D0  # the position last passed to SetToPosition (cTkPhysRelVec3)
PLAYER_SMOOTHED_POS = 0x3470  # cGcPlayer::mSmoothedPos (cTkPhysRelVec3)
CONTROLLER_ON_GROUND = 0x104  # cTkHavokCharacterController::mbOnGround; SetPosition's bool overwrites it
# The physics controller is placed by its capsule centre, which sits this far below mPosition. Measured on
# 180836 (a teleport to mPosition lifts the player by exactly this much); the follow checks it each frame.
CONTROLLER_BELOW_ROOT_M = 0.85
# Offsets of the calls inside SetToPosition we reuse: cTkHavokCharacterController::SetPosition(pos, false),
# cGcPlayer::SetFacing(dir) and cGcPlayer::UpdateGraphics(true). Verified against the disassembly on 180836.
SET_TO_POSITION_CALLS = {"ctrl_set_position": 0xD8, "ctrl_set_velocity": 0xE7, "set_facing": 0x10C,
                         "update_graphics": 0x116}
CONTROLLER_TARGET_VELOCITY = 0xE0  # cTkHavokCharacterController::mTargetVelocity (SetVelocity writes it)
# Frame order that matters for the follow: IVRCompositor::WaitGetPoses (pose job) -> cTkHmdOpenVR::ProcessPoses
# (multiplies head and hand poses by the inverse reset matrix, hands go to sTkOpenVRData::mmDeviceTransforms[1..2]
# with eight RIP-relative movaps stores) -> cTkInputManagerOpenVR::Update copies those into its input ports ->
# cGcPlayer::Update places the hands from the port copies -> camera. The follow therefore runs at the start of
# cTkInputManagerOpenVR::Update (game thread): it changes the inverse reset matrix, the two hand matrices and the
# body together, before anyone copies or renders them. (Changing the matrix on the pose job instead made the
# view flip during turns: the renderer reads it on its own schedule.)
PROCESS_POSES_SIG = "40 55 53 56 48 8D AC 24 ? ? ? ? 48 81 EC ? ? ? ? 48 8B F1 FF 15"
MOVAPS_RIP_STORE = rb"\x0f\x29[\x05\x0d\x15\x1d\x25\x2d](....)"
# cTkInputManagerOpenVR::Update copies them with cTkInputPort::SetDeviceTransform(port, type, available, matrix)
# into its port at +0x4C0: a 4x4 per device type at port+0x340+64*type (rows right, up, at, position).
INPUT_PORT = 0x4C0
PORT_DEVICE_TRANSFORMS = 0x340
HAND_DEVICE_TYPES = (1, 2)  # right, left
# cTkVector3* cGcPlayer::CalculatePlayerForce(out, bool): the Move stick (after its dead zone) as the world-space
# walking force, |force| <= 1 (4.13: GetDesiredControllerDir in the camera frame, turned to world). The caller,
# cGcPlayer::UpdateControl, stores it in mCurrentMoveForce and drives the physics controller with it;
# cGcPlayer::HasMoveForce counts |force|^2 >= 0.001 as moving. Current build: NMS+0x1464200.
CALC_FORCE_SIG = ("48 89 74 24 18 48 89 7C 24 20 41 56 48 81 EC ? ? ? ? 48 8B F1 45 0F B6 F0 48 8B 0D ? ? ? ? "
                  "48 8B FA 48 81 C1 ? ? ? ? 48 8B 01 FF 90 ? ? ? ? 84 C0 0F 85")
# cTkInputManagerOpenVR::Update: prologue, two calls (the second is cTkHmdOpenVR::GetInstance)
INPUT_UPDATE_SIG = (rb"\x48\x8b\xc4\x53\x41\x54\x48\x81\xec\x88\x00\x00\x00\x48\x89\x68\x08\x48\x8b\xd9\x48\x89\x70\x10"
                    rb"\x48\x89\x78\x18\x4c\x89\x78\xe8\xe8....\x45\x32\xff\xe8")
# cTkHmdOpenVR (the game's HMD object, from cTkHmdOpenVR::GetInstance)
HMD_STATE = 0x10  # eHmdSystemState: 5 = OpenVR running
HMD_INVERSE_RESET = 0x30  # cTkMatrix44, row vectors: head in body frame = raw pose * this
HMD_INVERSE_RESET_SET = 0x70  # bool, cleared while a recentre is pending
HMD_POSE = 0x2700  # cTkMatrix34, raw HMD pose in SteamVR's standing universe

_k32 = ctypes.WinDLL("kernel32")  # private instance, so the argtypes below don't leak to other mods
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_k32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]


def safe_read(addr: int, ctype):
    """Read a value without risking an access violation if a game update moved an offset."""
    out, n = ctype(), ctypes.c_size_t(0)
    if addr and _k32.ReadProcessMemory(_k32.GetCurrentProcess(), ctypes.c_void_p(addr), ctypes.byref(out),
                                       ctypes.sizeof(out), ctypes.byref(n)) and n.value == ctypes.sizeof(out):
        return out.value
    return None


# ------------------------------------------------------------------- OpenVR plumbing ----
TRACKING_UNIVERSE_STANDING = 1


class HmdMatrix34_t(ctypes.Structure):
    _fields_ = [("m", (ctypes.c_float * 4) * 3)]


class HmdVector3_t(ctypes.Structure):
    _fields_ = [("v", ctypes.c_float * 3)]


class TrackedDevicePose_t(ctypes.Structure):
    _fields_ = [
        ("mDeviceToAbsoluteTracking", HmdMatrix34_t),
        ("vVelocity", HmdVector3_t),
        ("vAngularVelocity", HmdVector3_t),
        ("eTrackingResult", ctypes.c_int32),
        ("bPoseIsValid", ctypes.c_bool),
        ("bDeviceIsConnected", ctypes.c_bool),
    ]


class InputPoseActionData_t(ctypes.Structure):
    _fields_ = [("bActive", ctypes.c_bool), ("activeOrigin", ctypes.c_uint64), ("pose", TrackedDevicePose_t)]


class InputAnalogActionData_t(ctypes.Structure):
    _fields_ = [("bActive", ctypes.c_bool), ("activeOrigin", ctypes.c_uint64), ("x", ctypes.c_float),
                ("y", ctypes.c_float), ("z", ctypes.c_float), ("deltaX", ctypes.c_float), ("deltaY", ctypes.c_float),
                ("deltaZ", ctypes.c_float), ("fUpdateTime", ctypes.c_float)]


# The game activates SteamVR action sets by what you are doing (GAMEDATA/INPUT/ACTIONS.JSON). The inventory and
# every other menu page use the FRONTEND sets (each also has _Left/_Right variants); which page cGcFrontendManager::
# Activate opened tells them apart. Every page stops the body turning except the hand menus (the inventories, which
# hang off your hand and turn with it). (Listing only the big-menu pages missed the teleporter and the build menu.)
# (eFrontendPage, NMS.py names.)
MENU_ACTION_SETS = ("/actions/FRONTEND",)
# Menus with action sets of their own (no FRONTEND page): the build menu's part pickers, the galaxy map, the photo
# mode menu. They stop the turning too. (The quick menus are hand menus; the build placement modes are not menus.)
OTHER_MENU_ACTION_SETS = ("/actions/BuildMenuSelectionMode", "/actions/BuildMenuBiggsSelect", "/actions/GalacticMap",
                          "/actions/PhotoModeMenu")
HAND_PAGES = ("Suit", "Ship", "Vehicle", "Freighter", "Weapon")
# Dialog screens: talking to someone, terminals, mission boards and the trade screens they lead to (own setting).
DIALOG_PAGES = ("Interact", "InteractDialog", "InteractConsole", "InteractShip", "IntermediateInteraction",
                "RadialInteraction", "Trade", "TechTrade", "BuildingTrade", "SpecialsTrade", "RepTrade", "CookTrade",
                "MissionList", "MissionHandInList", "MissionRenounce", "MissionDescription", "MissionDescription2",
                "Multiplayer_MissionList", "Multiplayer_MissionDescription")
try:
    from nmspy.data.enums.internal_enums import eFrontendPage as _eFrontendPage
except Exception:  # pragma: no cover - older NMS.py
    _eFrontendPage = None


def _page_name(page: int) -> str:
    try:
        return _eFrontendPage(page).name
    except Exception:
        return str(page)
MOVE_ACTION = b"/actions/OnFootControls/in/Move"  # the on-foot locomotion stick (GAMEDATA/INPUT/ACTIONS.JSON)


def _openvr():
    """The game's own openvr_api.dll, or None if VR is not initialised in this process."""
    dll = ctypes.WinDLL("openvr_api.dll")
    dll.VR_GetInitToken.restype = ctypes.c_uint32
    if dll.VR_GetInitToken() == 0:
        return None
    dll.VR_GetGenericInterface.restype = ctypes.c_void_p
    dll.VR_GetGenericInterface.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int32)]
    return dll


def hmd_position():
    """Raw HMD position in the standing (floor) universe, or None."""
    dll = _openvr()
    if dll is None:
        return None
    err = ctypes.c_int32(0)
    table = dll.VR_GetGenericInterface(b"FnTable:IVRSystem_026", ctypes.byref(err))
    if not table or err.value:
        return None
    fn = ctypes.cast(table, ctypes.POINTER(ctypes.c_void_p))[12]  # GetDeviceToAbsoluteTrackingPose
    poses = (TrackedDevicePose_t * 1)()
    ctypes.CFUNCTYPE(None, ctypes.c_int32, ctypes.c_float, ctypes.POINTER(TrackedDevicePose_t), ctypes.c_uint32)(fn)(
        TRACKING_UNIVERSE_STANDING, 0.0, poses, 1
    )
    p = poses[0]
    if not p.bPoseIsValid:
        return None
    m = p.mDeviceToAbsoluteTracking.m
    return (m[0][3], m[1][3], m[2][3])


# SteamVR client functions the mod hooks: {interface: {name: vtable slot}} (openvr.h, SteamVR 2.15)
VR_VTABLE_SLOTS = {
    b"IVRCompositor_029": {"WaitGetPoses": 2},
    b"IVRInput_011": {"GetActionSetHandle": 1, "GetActionHandle": 2, "UpdateActionState": 4, "GetAnalogActionData": 6,
                      "GetPoseActionDataRelativeToNow": 7, "GetPoseActionDataForNextFrame": 8},
    b"IVRChaperone_004": {"ResetZeroPose": 8},
}


def resolve_move_action(dll) -> int:
    """SteamVR handle of the on-foot Move action (0 if unavailable)."""
    err = ctypes.c_int32(0)
    obj = dll.VR_GetGenericInterface(b"IVRInput_011", ctypes.byref(err))
    if not obj or err.value:
        return 0
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[VR_VTABLE_SLOTS[b"IVRInput_011"]["GetActionHandle"]]
    handle = ctypes.c_uint64(0)
    rc = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64))(fn)(
        obj, MOVE_ACTION, ctypes.byref(handle))
    return handle.value if rc == 0 else 0


def resolve_action_sets(dll) -> dict:
    """{SteamVR action set handle: name} for every action set the game declares."""
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(_internal.BINARY_PATH)), "GAMEDATA", "INPUT", "ACTIONS.JSON")
        with open(path, encoding="utf-8-sig") as f:
            names = [a["name"] for a in json.load(f).get("action_sets", []) if "name" in a]
    except Exception:
        logger.exception("could not read the game's action sets; using the menu ones only")
        names = [n + sfx for n in MENU_ACTION_SETS for sfx in ("", "_Right", "_Left")]
    err = ctypes.c_int32(0)
    obj = dll.VR_GetGenericInterface(b"IVRInput_011", ctypes.byref(err))
    if not obj or err.value:
        return {}
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
    fn = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64))(
        ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[VR_VTABLE_SLOTS[b"IVRInput_011"]["GetActionSetHandle"]])
    out = {}
    for name in names:
        handle = ctypes.c_uint64(0)
        if fn(obj, name.encode(), ctypes.byref(handle)) == 0 and handle.value:
            out[handle.value] = name
    return out


_ACTION_SETS = {}  # filled with the SteamVR functions


def resolve_vr_funcs():
    try:
        dll = _openvr()
        if dll is None:
            return None
        out = {}
        for iface, slots in VR_VTABLE_SLOTS.items():
            err = ctypes.c_int32(0)
            obj = dll.VR_GetGenericInterface(iface, ctypes.byref(err))
            if not obj or err.value:
                return None
            vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0]
            fns = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))
            for name, slot in slots.items():
                out[name] = fns[slot]
        k32 = ctypes.windll.kernel32
        for name, addr in out.items():  # all of them must live in vrclient_x64.dll
            if name == "MoveAction":
                continue
            h = ctypes.c_void_p()
            buf = ctypes.create_unicode_buffer(260)
            if not k32.GetModuleHandleExW(0x6, ctypes.c_void_p(addr), ctypes.byref(h)):
                return None
            k32.GetModuleFileNameW(h, buf, 260)
            if "vrclient" not in buf.value.lower():
                logger.warning("%s resolved into %s, not vrclient - refusing", name, buf.value)
                return None
        out["MoveAction"] = resolve_move_action(dll)
        _ACTION_SETS.update(resolve_action_sets(dll))
        logger.info("SteamVR functions resolved: %s", {k: hex(v) for k, v in out.items()})
        return out
    except Exception:
        logger.exception("resolving SteamVR functions failed")
        return None


_VR_FUNCS = resolve_vr_funcs()  # None on a cold start (VR not up yet) -> the mod reloads itself later



# ------------------------------------------------------------------ game code we use ----
# Simple-interaction availability check, Chair case:
#   cmp eax,1Ah; jne ...; call <input manager getter>; cmp dword [rax+10h],5 (OpenVR); je <unavailable>
# The `je` (6 bytes) is NOPed so the normal seat checks run in VR too, and the getter is reused to
# find the input manager the recentre routine works on.
CHAIR_GATE_SIG = rb"\x83\xf8\x1a\x0f\x85....\xe8....\x83\x78\x10\x05(?=\x0f\x84|\x66\x0f\x1f\x44\x00\x00)"
CHAIR_GATE_CALL_OFFSET = 9
CHAIR_GATE_JE_OFFSET = 18
NOP6 = b"\x66\x0f\x1f\x44\x00\x00"
# cTkInputManagerOpenVR::Recentre: VR check, IVRChaperone::ResetZeroPose(seated), clears a flag,
# calls the game's recentre handler (HUD flash, body shift, new height reference).
GAME_RECENTER_SIG = "40 53 48 83 EC 20 83 79 10 05 48 8B D9 75 ? 48 8D 0D"
# cGcPlayer::UpdateGraphics writes the player's root node with cTkEngineUtils::SetNodeMatrixWorld:
#   mov ecx,[rbx+0E0h]; mov rdx,rdi; call <SetNodeMatrixWorld>; mov edx,[rbx+0E0h]
NODE_SET_SITE_SIG = rb"\x8b\x8b\xe0\x00\x00\x00\x48\x8b\xd7\xe8(....)\x8b\x93\xe0\x00\x00\x00"
# cGcPlayer::GetVRCameraOffset starts with  mov rax,[rcx+<cGcPlayer::mpThirdPerson>]  (0x148 on 180836)
VR_CAMERA_OFFSET_SIG = "48 8B 81 ? ? ? ? 48 85 C0 74 ? F3 0F 10 88 ? ? ? ? 0F 57 C0 0F 2E C8"
# void cGcPlayerThirdPerson::PostPhysicsUpdate(float) - places the character model every frame
THIRD_PERSON_POST_PHYSICS_SIG = "48 8B C4 55 53 57 48 8D A8 ? ? ? ? 48 81 EC ? ? ? ? 0F 29 70 D8 48 8B F9 44 0F 29 58 88"
# void cGcPlayer::SetToPosition(cTkPhysRelVec3* pos, cTkVector3* facing, cTkVector3* velocity) - the
# game's own player teleport (physics controller, smoothed position, graphics); NMS.py's signature.
SET_TO_POSITION_SIG = "48 89 5C 24 ? 48 89 6C 24 ? 48 89 74 24 ? 57 48 83 EC ? 48 8B FA 48 8B D9 48 8B 15"


def call_target(call: int) -> int:
    """Target of the `call rel32` instruction at `call` (checked to be one)."""
    if ctypes.string_at(call, 1) != b"\xe8":
        raise RuntimeError(f"no call instruction at 0x{call:X} (game updated?)")
    return call + 5 + ctypes.c_int32.from_address(call + 1).value


def ida_to_regex(sig: str) -> bytes:
    """"48 8B ? C3" -> a regex for find_unique (? = any byte)."""
    return b"".join(b"." if b == "?" else re.escape(bytes([int(b, 16)])) for b in sig.split())


def find_unique(sig: bytes) -> tuple:
    """(absolute address, match) of a regex signature that must occur exactly once in .text."""
    pe = pefile.PE(_internal.BINARY_PATH, fast_load=True)
    text = next(s for s in pe.sections if s.Name.rstrip(b"\0") == b".text")
    start = _internal.BASE_ADDRESS + text.VirtualAddress
    hits = list(re.finditer(sig, ctypes.string_at(start, text.Misc_VirtualSize), re.DOTALL))
    if len(hits) != 1:
        raise RuntimeError(f"signature matched {len(hits)} times (game updated?)")
    return start + hits[0].start(), hits[0]


def write_code(addr: int, data: bytes) -> None:
    k32 = ctypes.windll.kernel32
    old = ctypes.c_ulong(0)
    if not k32.VirtualProtect(ctypes.c_void_p(addr), len(data), 0x40, ctypes.byref(old)):
        raise OSError("VirtualProtect failed")
    ctypes.memmove(addr, data, len(data))
    k32.VirtualProtect(ctypes.c_void_p(addr), len(data), old, ctypes.byref(old))
    k32.FlushInstructionCache(ctypes.c_void_p(-1), ctypes.c_void_p(addr), len(data))


def _find_input_update() -> int:
    """RVA of cTkInputManagerOpenVR::Update, or 0 (the follow then runs from cGcPlayer::Update instead)."""
    try:
        addr, _ = find_unique(INPUT_UPDATE_SIG)
        return addr - _internal.BASE_ADDRESS
    except Exception:
        logger.exception("cTkInputManagerOpenVR::Update not found; the hands may lag the body by a frame")
        return 0


_INPUT_UPDATE = _find_input_update()


def _find_calc_force() -> int:
    """RVA of cGcPlayer::CalculatePlayerForce, or 0 (then the follow walks by stick emulation)."""
    try:
        addr, _ = find_unique(ida_to_regex(CALC_FORCE_SIG))
        return addr - _internal.BASE_ADDRESS
    except Exception:
        logger.exception("cGcPlayer::CalculatePlayerForce not found; walking by stick emulation instead")
        return 0


_CALC_FORCE = _find_calc_force()


def _find_camera_update() -> int:
    """RVA of cGcCameraBehaviourFirstPerson::Update: slot 4 of the class's vftable, found through its RTTI."""
    try:
        pe = pefile.PE(_internal.BINARY_PATH, fast_load=True)
        base = _internal.BASE_ADDRESS
        rdata = next(s for s in pe.sections if s.Name.rstrip(b"\0") == b".rdata")
        data = next(s for s in pe.sections if s.Name.rstrip(b"\0") == b".data")
        # TypeDescriptor: vftable ptr, spare, ".?AVcGcCameraBehaviourFirstPerson@@"
        name = b".?AVcGcCameraBehaviourFirstPerson@@\0"
        blob = ctypes.string_at(base + data.VirtualAddress, data.Misc_VirtualSize)
        td = blob.find(name)
        if td < 0:
            raise RuntimeError("type descriptor not found")
        td_rva = data.VirtualAddress + td - 16
        # Complete Object Locator (in .rdata) referencing this type descriptor with offset 0
        rblob = ctypes.string_at(base + rdata.VirtualAddress, rdata.Misc_VirtualSize)
        col_rva = 0
        for mm in re.finditer(re.escape(td_rva.to_bytes(4, "little")), rblob):
            o = mm.start() - 12  # signature, offset, cdOffset, pTypeDescriptor
            if o >= 0 and rblob[o:o + 8] == b"\x01\x00\x00\x00\x00\x00\x00\x00":
                col_rva = rdata.VirtualAddress + o
                break
        if not col_rva:
            raise RuntimeError("complete object locator not found")
        # the vftable is preceded by a pointer to its COL
        col_va = (base + col_rva).to_bytes(8, "little")
        i = rblob.find(col_va)
        if i < 0:
            raise RuntimeError("vftable not found")
        vft = base + rdata.VirtualAddress + i + 8
        fn = ctypes.c_uint64.from_address(vft + 8 * 4).value
        if ctypes.string_at(fn, 4) != b"\xf3\x0f\x11\x4c":  # movss [rsp+..],xmm1
            raise RuntimeError("slot 4 does not look like Update (game updated?)")
        return fn - base
    except Exception:
        logger.exception("cGcCameraBehaviourFirstPerson::Update not found; the view cannot be anchored")
        return 0


_CAMERA_UPDATE = _find_camera_update()

# cTkPhysRelVec3* cGcPlayer::FootPos(out): the physics controller's live position minus 1 m along up (vehicle
# matrix when riding). In VR the first-person camera is FootPos + GetVRCameraOffset, read after the player update.
FOOT_POS_SIG = ("48 89 5C 24 08 57 48 81 EC ? ? ? ? 48 8B 81 ? ? ? ? 48 8B DA 45 32 C0 48 8B F9 48 8B 90 ? ? ? ? "
                "48 85 D2 74 ? 48 8B 8A ? ? ? ? 48 85 C9 74 ? 48 8B 01 FF 50 30")


def _find_foot_pos() -> int:
    try:
        addr, _ = find_unique(ida_to_regex(FOOT_POS_SIG))
        return addr
    except Exception:
        logger.exception("cGcPlayer::FootPos not found: the view anchor falls back to mPosition")
        return 0


_FOOT_POS = _find_foot_pos()
_FOOT_POS_FN = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(_FOOT_POS) if _FOOT_POS else None


# ------------------------------------------------------------------------------- state ----
@dataclass
class SitState(ModState):
    game_sit: int = 0  # the game's cGcCharacterSit::meState for the local player
    phys_seated: bool = False  # the user sits physically (detected from the headset)
    head_peak: tuple = (0.0, 0.0)  # (height, time) of the recent highest head position
    settle_pos: tuple | None = None
    settle_since: float = 0.0
    hmd_pos: tuple | None = None
    inject_until: float = 0.0
    last_sample: float = 0.0
    reload_requested: bool = False
    walk_map: tuple | None = None  # 2x2 mapping body-frame direction -> Move stick, from the self-calibration


_LIVE_INSTANCE = "_vr_sit_live_instance"  # kept on sys, which survives this module being reloaded


class VRSit(Mod):
    __author__ = "Traveller"
    __description__ = "Sit in chairs in VR: enables the prompt, recentres at seat height, fixes the floating body"
    __version__ = "1.2"

    state = SitState()
    _pose_dy = 0.0  # vertical offset currently applied to every pose the game reads

    def __init__(self):
        super().__init__()
        setattr(sys, _LIVE_INSTANCE, self)  # older instances (hot reloads) go quiet from here on
        self._input_mgr_getter = 0
        self._chair_je = 0  # the je of the "no chairs in VR" check
        self._game_recenter = 0
        self._set_node_world = None
        self._third_person_ptr = 0  # offset of cGcPlayer::mpThirdPerson
        self._anchor_errors = 0
        self._player = 0
        self._body_drop = 0.0
        self._last_body_write = None
        self._move_ready = False
        self._hmd = 0
        self._followed = 0.0  # metres the body was moved since the last log line
        self._follow_log_at = 0.0
        self._lift_check = [0, 0.0, 0, 0.0]  # self-check: frames after a step, their up-motion, free frames, theirs
        self._prev_frame_pos = None
        self._stepped_last = False
        self._turning = False  # the body is catching up with the head's yaw (hysteresis)
        self._turned = 0.0  # degrees turned since the last log line
        self._pending = None  # (world shift, new facing) planned, then applied by _apply_follow
        self._last_update = 0.0  # when cGcPlayer::Update last ran
        self._last_plan = 0.0
        self._input_mgr = 0  # cTkInputManagerOpenVR, from its Update hook
        self._pos_before = None  # mPosition when the frame's follow was planned (before physics)
        self._walking_until = 0.0  # the view is corrected for the body's walk until then
        self._walk_ok = True
        self._walked = 0.0  # metres of walk taken out of the view since the last log line
        self._body_vel = (0.0, 0.0)  # the body's velocity in its own frame (right, at), m/s
        self._vel_time = 0.0
        self._cmd_dir = None  # unit stick direction last commanded (for the mapping adaptation)
        self._walk_log_at = 0.0
        self._blocked_frames = 0
        self._striding = False  # the body is on its way to under the head
        self._force_cmd = None  # world-space walking force for this frame (force mode), or None
        self._force_writes = 0
        self._plan_g = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        self._head_at_body = (0.0, 0.0, 1.0)
        self._hold_ok = True
        self._anchor = None  # world position of the tracking origin the view is held to
        self._hold_base = None
        self._base_hist = []
        self._held = False
        self._held_m = 0.0  # metres of body motion the anchor absorbed since the last log line
        self._camera_frames = 0
        self._camera_seen_at = 0.0
        self._cam_prev = None  # camera / raw head motion stats for the follow log line
        self._raw_prev = None
        self._cam_stats = [0.0, 0.0, 0.0, 0.0]  # max camera step, max raw head step, sums
        self._jump_log = 0
        self._release_log = 0
        self._recentred_at = 0.0
        self._predicted = (0.0, 0.0)  # body-frame shift already taken out of the view before physics
        self._stick_user = (0.0, 0.0)  # what you are doing with the Move stick
        self._stick_time = 0.0
        self._user_force_at = 0.0  # when the game last produced a walking force from your stick
        self._menu_active = False  # a menu action set is active (SteamVR UpdateActionState)
        self._menu_seen_at = 0.0
        self._other_menu_active = False  # a menu with its own action set (build menu, galaxy map, photo mode)
        self._frontend_page = -1  # the page cGcFrontendManager::Activate last opened
        self._action_sets = ()
        self._action_set_logs = 0
        self._stride_dir = None  # world direction the current stride set out in
        self._stick_active_at = 0.0  # when you last pushed it
        self._stick_cmd = None  # stick vector the follow wants the game to see (None = leave it alone)
        self._stick_written = None  # what we last wrote into the game's stick data
        # 16-byte aligned scratch for the vectors passed to SetToPosition (the game uses SSE loads)
        self._vec_buf = (ctypes.c_ubyte * 96)()
        self._vec_base = (ctypes.addressof(self._vec_buf) + 15) & ~15
        try:
            addr = _internal.BASE_ADDRESS + MAX_HORIZONTAL_VR if MAX_HORIZONTAL_VR else 0
            old = safe_read(addr, ctypes.c_float) if addr else None
            if old is not None and 0.1 <= old <= 100.0:  # sanity: it is 0.75 on 180836
                ctypes.c_float.from_address(addr).value = MAX_HEAD_OFFSET_M
                logger.info("MaximumHorizontalOffsetVR %.2f -> %.1f m", old, MAX_HEAD_OFFSET_M)
            else:
                logger.warning("MaximumHorizontalOffsetVR not where expected (read %s); leaving it", old)
            # Its two neighbours, MaximumCrouchVR (-8) and MaximumHeadHeightIncreaseVR (-4), feed the same check
            # (cGcPlayerThirdPerson::PostPhysicsUpdate -> mbOutsidePlayArea +0x153), which only raises the "moved
            # outside of the play area" warning. This mod opens those limits too, so the warning no longer appears
            # while the body follows you.
            for off, lim, default in ((-8, -1000.0, -1.25), (-4, 1000.0, 0.5)):
                v = safe_read(addr + off, ctypes.c_float) if addr else None
                if v is not None and (abs(v - default) < 1e-4 or v == lim):
                    ctypes.c_float.from_address(addr + off).value = lim
                else:
                    logger.warning("VR head-height limit at %+d not where expected (read %s); leaving it", off, v)
        except Exception:
            logger.exception("could not lift MaximumHorizontalOffsetVR")
        try:
            gate, _ = find_unique(CHAIR_GATE_SIG)
            self._chair_je = gate + CHAIR_GATE_JE_OFFSET
            self._apply_chair_gate()
            call = gate + CHAIR_GATE_CALL_OFFSET
            self._input_mgr_getter = call + 5 + ctypes.c_int32.from_address(call + 1).value
        except Exception:
            logger.exception("chair VR gate patch failed")
        try:
            self._game_recenter = _internal.BASE_ADDRESS + find_pattern_in_binary(GAME_RECENTER_SIG, False)
        except Exception:
            logger.exception("game recentre routine not found")
        try:
            site, _ = find_unique(NODE_SET_SITE_SIG)
            call = site + 9
            target = call + 5 + ctypes.c_int32.from_address(call + 1).value
            self._set_node_world = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_void_p)(target)
            fn = _internal.BASE_ADDRESS + find_pattern_in_binary(VR_CAMERA_OFFSET_SIG, False)
            self._third_person_ptr = ctypes.c_int32.from_address(fn + 3).value
            logger.info("body correction ready (SetNodeMatrixWorld NMS+0x%X, player->third person +0x%X)",
                        target - _internal.BASE_ADDRESS, self._third_person_ptr)
        except Exception:
            logger.exception("body correction unavailable (game code not found)")
        try:
            fn = _internal.BASE_ADDRESS + find_pattern_in_binary(SET_TO_POSITION_SIG, False)
            c = SET_TO_POSITION_CALLS
            # `mov rcx,[rbx+<mpPhysicsController>]` right before the controller call: layout check
            if ctypes.string_at(fn + c["ctrl_set_position"] - 13, 7) != b"\x48\x8b\x8b" + PLAYER_CONTROLLER.to_bytes(4, "little"):
                raise RuntimeError("SetToPosition does not look as expected (game updated?)")
            t = {k: call_target(fn + v) for k, v in c.items()}
            self._ctrl_set_position = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint8)(t["ctrl_set_position"])
            self._set_facing = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(t["set_facing"])
            self._update_graphics = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint8)(t["update_graphics"])
            self._move_ready = True
            logger.info("body follow ready (from SetToPosition NMS+0x%X: %s)", fn - _internal.BASE_ADDRESS,
                        {k: "NMS+0x%X" % (v - _internal.BASE_ADDRESS) for k, v in t.items()})
        except Exception:
            logger.exception("body follow unavailable (SetToPosition not found)")
        self._hand_transforms = []
        try:
            fn = _internal.BASE_ADDRESS + find_pattern_in_binary(PROCESS_POSES_SIG, False)
            code = ctypes.string_at(fn, 0x1200)
            targets = sorted({fn + mm.end() + int.from_bytes(mm.group(1), "little", signed=True)
                              for mm in re.finditer(MOVAPS_RIP_STORE, code, re.DOTALL)})
            runs = [t for t in targets if all(t + 16 * i in targets for i in range(8))]
            if len(runs) != 1:
                raise RuntimeError(f"hand matrix stores not as expected: {[hex(t) for t in targets]}")
            self._hand_transforms = [runs[0], runs[0] + 64]
            logger.info("hand matrices at NMS+0x%X and NMS+0x%X (from ProcessPoses NMS+0x%X)",
                        runs[0] - _internal.BASE_ADDRESS, runs[0] + 64 - _internal.BASE_ADDRESS, fn - _internal.BASE_ADDRESS)
        except Exception:
            logger.exception("hand matrices not found: the hands will lag the body by a frame while it follows you")

    # ------------------------------------------------ the body follows you around the room ----
    def _follow_frame(self):
        """One frame of follow: plan from the HMD pose the game processed for this frame, apply."""
        if not self._hmd or not self._move_ready or not self._player:
            return
        try:
            at = struct.unpack("3f", ctypes.string_at(self._hmd + HMD_POSE + 32, 12))
            pos = struct.unpack("3f", ctypes.string_at(self._hmd + HMD_POSE + 48, 12))
            self._plan_follow(at, pos)
            self._apply_follow(self._player)
            if not self._camera_hold_live():
                # fallback (no first-person camera update seen): hold against mPosition here
                self._hold_view(struct.unpack("3f", ctypes.string_at(self._player + PLAYER_POSITION, 12)), "position")
            self._pos_before = struct.unpack("3f", ctypes.string_at(self._player + PLAYER_POSITION, 12))
            self._predicted = (0.0, 0.0)
            if WALK_PREDICT and time.monotonic() < self._walking_until and not self._stick_active():
                # Take this frame's expected walk out of the view before physics; the residual is fixed after.
                vx, vz = self._body_vel
                dt = min(max(time.monotonic() - self._last_plan, 0.0), 0.05)
                px_, pz_ = vx * dt, vz * dt
                if abs(px_) + abs(pz_) > 1e-7:
                    row = (ctypes.c_float * 3).from_address(self._hmd + HMD_INVERSE_RESET + 48)
                    row[0] -= px_
                    row[2] -= pz_
                    self._predicted = (px_, pz_)
        except Exception:
            logger.exception("body follow failed; disabling it")
            self._move_ready = False

    if _CAMERA_UPDATE:
        @manual_hook("cGcCameraBehaviourFirstPerson::Update", offset=_CAMERA_UPDATE,
                     func_def=FUNCDEF(None, [ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p]), detour_time="before")
        def before_camera_update(self, this, dt, arg):
            if not self._live():
                return
            self._camera_frames += 1
            self._camera_seen_at = time.monotonic()
            # The VR camera is built right after this from cGcPlayer::FootPos (the physics controller's live
            # position, already updated by this frame's player update) and the stereo views read the inverse
            # reset matrix after that: solve the anchor here, from exactly that position.
            # (An exception escaping this hook makes pyMHF switch it off for good, so none may.)
            if _FOOT_POS_FN and self._player and self._hold_ok:
                try:
                    fp = self._foot_pos(self._player)
                    if fp is not None:
                        self._hold_view(fp, "foot")
                except Exception:
                    if self._anchor_errors < 3:
                        logger.exception("view anchor skipped a frame")
                    self._anchor_errors += 1
            if arg and self._hmd:  # stats: the camera matrix (previous frame's result) vs the raw HMD motion
                try:
                    v = struct.unpack("20f", ctypes.string_at(int(arg), 80))
                    cam = (v[12] + v[16], v[13] + v[17], v[14] + v[18])
                    raw = struct.unpack("3f", ctypes.string_at(self._hmd + HMD_POSE + 48, 12))
                    if self._cam_prev is not None:
                        dc = math.dist(cam, self._cam_prev)
                        dr = math.dist(raw, self._raw_prev)
                        self._cam_stats[0] = max(self._cam_stats[0], dc)
                        self._cam_stats[1] = max(self._cam_stats[1], dr)
                        self._cam_stats[2] += dc
                        self._cam_stats[3] += dr
                    self._cam_prev, self._raw_prev = cam, raw
                except Exception:
                    pass

    def _foot_pos(self, player: int):
        """The position the VR camera is built on this frame (cGcPlayer::FootPos), world space - or None while
        the player has no body or physics controller (loading screens, warps: FootPos would crash then)."""
        tp = safe_read(player + self._third_person_ptr, ctypes.c_uint64) if self._third_person_ptr else None
        ctrl = safe_read(player + PLAYER_CONTROLLER, ctypes.c_uint64)
        if not tp or not ctrl or not safe_read(ctrl + 0xC0, ctypes.c_uint64):  # GetPosition: controller -> body
            return None
        buf = (ctypes.c_float * 12)()
        b = (ctypes.addressof(buf) + 15) & ~15
        _FOOT_POS_FN(player, b)
        v = struct.unpack("8f", ctypes.string_at(b, 32))
        return (v[0] + v[4], v[1] + v[5], v[2] + v[6])

    def _camera_hold_live(self) -> bool:
        return bool(_FOOT_POS_FN) and time.monotonic() - self._camera_seen_at < 0.25

    def _hold_view(self, base, kind: str):
        """Right before the camera reads the body: keep the tracking origin's world position (the anchor)
        where it was, by re-solving the head-reset matrix's horizontal translation from the position the
        camera is built on (base). The anchor follows the body only while you move with the stick, right
        after a recentre, when the game teleports you, or when the follow is off (seated, vehicle)."""
        player, hmd = self._player, self._hmd
        if not VIEW_HOLD or not self._hold_ok or not player or not hmd:
            return
        if kind != getattr(self, "_hold_kind", None):  # another base point: re-anchor, never jump
            self._hold_kind, self._anchor, self._hold_base = kind, None, None
        g = struct.unpack("20f", ctypes.string_at(player + PLAYER_GRAPHICS_MATRIX, 80))
        nr = math.sqrt(sum(v * v for v in g[0:3])) or 1.0
        na = math.sqrt(sum(v * v for v in g[8:11])) or 1.0
        right, at = [v / nr for v in g[0:3]], [v / na for v in g[8:11]]
        row = (ctypes.c_float * 3).from_address(hmd + HMD_INVERSE_RESET + 48)
        now = time.monotonic()
        jumped = self._hold_base is not None and math.dist(base, self._hold_base) > VIEW_HOLD_TELEPORT_M
        self._hold_base = base
        conds = (self._on_foot(player), not self._stick_active(), not jumped,
                 now - self._stick_active_at > WALK_SETTLE_S,  # your own walk has settled
                 now - self._recentred_at > VIEW_HOLD_RECENTER_S,
                 safe_read(hmd + HMD_STATE, ctypes.c_int32) == 5, bool(safe_read(hmd + HMD_INVERSE_RESET_SET, ctypes.c_uint8)))
        hold = all(conds)
        if not hold and self._held and self._release_log < 30:  # why the anchor let go (first 30 times)
            self._release_log += 1
            logger.info("anchor released: onfoot %s stickidle %s nojump %s settled %s norecentre %s vr %s flag %s | stick %s written %s",
                        *conds, self._stick_user, self._stick_written)
        if not hold or self._anchor is None:
            # follow the body: the anchor is wherever the tracking origin is right now
            self._anchor = [base[i] + row[0] * right[i] + row[2] * at[i] for i in range(3)]
            self._held = hold
            self._base_hist = [base]
            return
        self._held = True
        # The renderer pairs the head-reset matrix with a camera matrix that may be a frame old: solve
        # against the body position from VIEW_HOLD_LAG_FRAMES camera frames ago.
        self._base_hist.append(base)
        del self._base_hist[:-4]
        ref = self._base_hist[max(0, len(self._base_hist) - 1 - VIEW_HOLD_LAG_FRAMES)]
        d = [self._anchor[i] - ref[i] for i in range(3)]
        cx = sum(d[i] * right[i] for i in range(3))
        cz = sum(d[i] * at[i] for i in range(3))
        if abs(cx - row[0]) + abs(cz - row[2]) > 1e-6:
            self._held_m += math.hypot(cx - row[0], cz - row[2])
            row[0], row[2] = cx, cz

    if _CALC_FORCE:
        @manual_hook("cGcPlayer::CalculatePlayerForce", offset=_CALC_FORCE,
                     func_def=FUNCDEF(ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint8]),
                     detour_time="after")
        def on_player_force(self, this, out, flatten):
            if not self._live():
                return
            # The game just turned the Move stick into this frame's walking force: while you are not using
            # the stick, the follow's force walks the body under your head instead (any size, no dead zone).
            if not out or not this or int(this) != self._player:
                return
            v = (ctypes.c_float * 3).from_address(int(out))
            if not WALK_BY_STICK and v[0] * v[0] + v[1] * v[1] + v[2] * v[2] >= 0.001:
                # A force of the game's own (past its stick dead zone, the same test as cGcPlayer::HasMoveForce):
                # you are walking with the stick. Exactly what the game counts, so a drifting stick never does.
                self._user_force_at = self._stick_active_at = time.monotonic()
                return
            f = self._force_cmd
            if f is None or self._stick_active():
                return
            self._force_writes += 1
            v[:] = f

    @nms.cGcFrontendManager.Activate.after
    def on_frontend_activate(self, this, lePage, lbTransitionRight):
        if not self._live():
            return
        page = int(lePage) if lePage is not None else -1
        if page != self._frontend_page:
            logger.info("menu page opened: %s%s", _page_name(page),
                        " (the body stops turning)" if self._page_blocks_turn(page) else "")
        self._frontend_page = page

    if _INPUT_UPDATE:
        @manual_hook("cTkInputManagerOpenVR::Update", offset=_INPUT_UPDATE, func_def=FUNCDEF(None, [ctypes.c_void_p]),
                     detour_time="before")
        def before_input_update(self, this):
            if not self._live():
                return
            # Only to learn where the input manager (and so its port's copies of the hand poses) lives.
            # Running the follow here made the view flip: the renderer picks the matrix up between this
            # update and the player update.
            self._input_mgr = int(this) if this else 0

    @nms.cGcPlayer.Update.before
    def follow_update(self, this: ctypes._Pointer[nms.cGcPlayer], lfStep: float):
        if not self._live():
            return
        self._player = get_addressof(this)
        self._last_update = time.monotonic()
        if not self._hmd and self._input_mgr_getter:
            self._hmd = ctypes.CFUNCTYPE(ctypes.c_void_p)(self._input_mgr_getter)() or 0
        self._follow_frame()

    # ------------------------------------------------------------------- settings ----
    def _set_setting(self, name: str, value) -> None:
        """A setting changed in the pyMHF window: apply it, save it."""
        global _settings_mtime
        try:
            v = _coerce_setting(name, value)
        except Exception:
            return
        if globals()[name] == v:
            return
        globals()[name] = v
        _settings_mtime = save_settings()
        logger.info("setting %s = %s", name, v)
        self._apply_settings()

    def _watch_settings(self) -> None:
        """Pick up hand edits of the settings file."""
        global _settings_mtime
        m = _mtime(SETTINGS_FILE)
        if m and m != _settings_mtime:
            _settings_mtime = load_settings()
            logger.info("settings re-read from %s", SETTINGS_FILE)
            self._apply_settings()

    def _apply_settings(self) -> None:
        """Side effects of settings that are more than a value read every frame."""
        self._apply_chair_gate()

    def _apply_chair_gate(self) -> None:
        addr = self._chair_je
        if not addr:
            return
        try:
            cur = ctypes.string_at(addr, 6)
            if CHAIRS_IN_VR:
                if cur != NOP6:
                    setattr(sys, "_vr_sit_chair_je", cur)  # sys survives reloads: the original for switching off
                    write_code(addr, NOP6)
                    logger.info("chairs in VR: on (gate patched at 0x%X)", addr)
            elif cur == NOP6:
                orig = getattr(sys, "_vr_sit_chair_je", None)
                if orig and orig[:2] == b"\x0f\x84":
                    write_code(addr, orig)
                    logger.info("chairs in VR: off (original check restored)")
                else:
                    logger.warning("chairs in VR stay on until the game restarts (original code not known)")
        except Exception:
            logger.exception("switching chairs in VR failed")

    def _live(self) -> bool:
        """Only the newest instance may act. pyMHF's reload removes only the hooks it registered first on a
        function, so detours on functions other mods hook too (cGcPlayer::Update) stay attached to the old
        instance: after the VR-up self-reload, a stale copy kept holding the view while you used the stick."""
        return getattr(sys, _LIVE_INSTANCE, None) is self

    def _on_foot(self, player: int) -> bool:
        return self.state.game_sit == 0 and safe_read(player + PLAYER_VEHICLE, ctypes.c_uint64) == 0

    @staticmethod
    def _page_blocks_turn(page: int) -> bool:
        name = _page_name(page)
        if name in HAND_PAGES:
            return False
        return DIALOG_NO_TURN if name in DIALOG_PAGES else MENU_NO_TURN

    def _turn_blocked(self) -> bool:
        """A menu that is not a hand menu is open: the FRONTEND action sets are active and the page the game
        opened is not an inventory, or one of the other menus' action sets is active."""
        if time.monotonic() - self._menu_seen_at >= 0.5:
            return False
        return (self._menu_active and self._page_blocks_turn(self._frontend_page)) \
            or (self._other_menu_active and MENU_NO_TURN)

    def _stick_active(self) -> bool:
        """You are moving with the stick: the follow keeps its hands off the moving controller."""
        now = time.monotonic()
        if now - self._user_force_at < 0.25:
            return True
        x, y = self._stick_user
        return math.hypot(x, y) > STICK_DEADZONE and now - self._stick_time < 0.25

    def _move_player(self, player: int, d, up) -> bool:
        """Shift the player by the world vector d: the physics controller (placed by its capsule centre,
        below the root position) and the player's own position fields, all relative. Velocity, facing,
        momentum and the fallen-through-floor check are left alone, unlike cGcPlayer::SetToPosition."""
        ctrl = safe_read(player + PLAYER_CONTROLLER, ctypes.c_uint64)
        if not ctrl:
            return False
        pos = struct.unpack("3f", ctypes.string_at(player + PLAYER_POSITION, 12))
        off = struct.unpack("3f", ctypes.string_at(player + PLAYER_GRAPHICS_MATRIX + 64, 12))
        vec = (ctypes.c_float * 8).from_address(self._vec_base)
        vec[0:4] = [pos[i] - off[i] - CONTROLLER_BELOW_ROOT_M * up[i] + d[i] for i in range(3)] + [0.0]
        vec[4:8] = [off[0], off[1], off[2], 1.0]
        # SetPosition(pos, lbOnGround) also stores its flag; hand the current one back so a step never
        # flips the character into its "in the air" state.
        on_ground = safe_read(ctrl + CONTROLLER_ON_GROUND, ctypes.c_uint8) or 0
        self._ctrl_set_position(ctrl, self._vec_base, 1 if on_ground else 0)
        for o in (PLAYER_POSITION, PLAYER_SMOOTHED_POS, PLAYER_LAST_POSITION, PLAYER_SET_POSITION):
            f = (ctypes.c_float * 3).from_address(player + o)
            f[:] = [f[i] + d[i] for i in range(3)]
        self._update_graphics(player, 1)
        return True

    def _plan_follow(self, at_row, pos_row):
        """Decide this frame's follow from the HMD pose (its "at" row and position in tracking space) and
        apply the head-reset-matrix side of it; the body side goes to self._pending for _apply_follow."""
        self._stick_cmd = None  # set again below if the body should walk this frame
        self._force_cmd = None
        if not (FOLLOW_MOVE or FOLLOW_YAW) or not self._move_ready or self._pending is not None:
            return
        player, hmd = self._player, self._hmd
        now = time.monotonic()
        if not player or not hmd:
            return
        if safe_read(hmd + HMD_STATE, ctypes.c_int32) != 5 or not safe_read(hmd + HMD_INVERSE_RESET_SET, ctypes.c_uint8) \
                or not self._on_foot(player):
            return
        stick = self._stick_active()  # you walk with the stick: the body still turns, but takes no strides
        if stick:
            self._striding = False  # your own walk cancels a stride; the next one needs the full start distance
        dt = min(max(now - self._last_plan, 0.0), 0.1)
        self._last_plan = now
        ax, ay, az = at_row  # the device's +z axis, the game's "at" row
        px, py, pz = pos_row
        rows = (ctypes.c_float * 16).from_address(hmd + HMD_INVERSE_RESET)
        m = rows[:]
        g = struct.unpack("20f", ctypes.string_at(player + PLAYER_GRAPHICS_MATRIX, 80))
        nr = math.sqrt(sum(v * v for v in g[0:3])) or 1.0
        na = math.sqrt(sum(v * v for v in g[8:11])) or 1.0
        right, at = [v / nr for v in g[0:3]], [v / na for v in g[8:11]]
        nu = math.sqrt(sum(v * v for v in g[4:7])) or 1.0
        self._plan_g = right + [v / nu for v in g[4:7]] + at  # normalised rows, for the force frame
        new_at = None
        # The head's "at" row in the body frame; the game's own recentre aligns it with +z.
        hx = ax * m[0] + ay * m[4] + az * m[8]
        hy = ax * m[1] + ay * m[5] + az * m[9]
        hz = ax * m[2] + ay * m[6] + az * m[10]
        self._head_at_body = (hx, hy, hz)
        if FOLLOW_YAW and not self._turn_blocked():
            if math.hypot(hx, hz) >= 0.2:  # otherwise you look almost straight up or down: no yaw
                yaw = math.degrees(math.atan2(hx, hz))
                if self._turning:
                    self._turning = abs(yaw) >= YAW_ALIGNED_DEG
                else:
                    self._turning = abs(yaw) >= YAW_DEADBAND_DEG
                if self._turning:
                    step = max(-YAW_RATE_DEG * dt, min(YAW_RATE_DEG * dt, yaw))
                    c, s = math.cos(math.radians(step)), math.sin(math.radians(step))
                    for r in range(4):  # M' = M x yaw(step): rotates every row's (x, z)
                        x, z = m[4 * r], m[4 * r + 2]
                        m[4 * r], m[4 * r + 2] = x * c - z * s, x * s + z * c
                    right, at = ([c * right[i] - s * at[i] for i in range(3)],
                                 [s * right[i] + c * at[i] for i in range(3)])  # the body, turned the same
                    new_at = at
                    self._turned += step
                    self._plan_g = right + self._plan_g[3:6] + at
                    hx, hz = hx * c - hz * s, hx * s + hz * c  # the head's at row in the turned frame
                    self._head_at_body = (hx, hy, hz)
        # The head's position in the (turned) body frame: walk the body there (the view is corrected after
        # physics by what the walk moved it), or take it out of the matrix and teleport the body by it.
        hx = px * m[0] + py * m[4] + pz * m[8] + m[12]
        hz = px * m[2] + py * m[6] + pz * m[10] + m[14]
        dist = math.hypot(hx, hz)
        d = None
        if not FOLLOW_MOVE or stick:
            pass
        elif FOLLOW_WALK and self._walk_ok:
            self._plan_walk(hx, hz, dist, now)
        elif FOLLOW_DEADBAND_M <= dist < 50.0:
            k = min(1.0, FOLLOW_MAX_SPEED * dt / dist)
            dx, dz = hx * k, hz * k
            m[12] -= dx
            m[14] -= dz
            d = [dx * right[i] + dz * at[i] for i in range(3)]
            self._followed += dist * k
        if new_at is None and d is None:
            return
        rows[:] = m
        # The hand poses of this frame already carry the old matrix: the shared ones (ProcessPoses' output)
        # and the input port's copies the player update is about to read. Same change for all of them.
        hands = list(self._hand_transforms)
        if self._input_mgr:
            hands += [self._input_mgr + INPUT_PORT + PORT_DEVICE_TRANSFORMS + 64 * t for t in HAND_DEVICE_TYPES]
        for addr in hands:
            t = (ctypes.c_float * 16).from_address(addr)
            if new_at is not None:
                for r in range(4):
                    x, z = t[4 * r], t[4 * r + 2]
                    t[4 * r], t[4 * r + 2] = x * c - z * s, x * s + z * c
            if d is not None:
                t[12] -= dx
                t[14] -= dz
        self._pending = (d, new_at)

    # ------------------------------------------------------------ walking instead of teleporting ----
    def _plan_walk(self, hx: float, hz: float, dist: float, now: float):
        """Walk the body under the head: write the game's walking force (force mode) or push the Move stick."""
        force = not WALK_BY_STICK and bool(_CALC_FORCE)
        if dist >= 50.0:
            return
        if now - self._stick_active_at < WALK_SETTLE_S:
            self._striding = False  # your own walk is still coasting out (and the view anchor with it)
            return
        # Aim at where the head will be relative to the body shortly: the body's own velocity (from the
        # last physics step) lets it ease off instead of overshooting and oscillating around the head.
        lead = FORCE_LEAD_S if force else WALK_LEAD_S
        # The target is a point FOLLOW_BEHIND_M behind the head (the body's forward is -at, so behind is +at).
        hz += FOLLOW_BEHIND_M
        dist = math.hypot(hx, hz)
        vx, vz = self._body_vel
        hx, hz = hx - vx * lead, hz - vz * lead
        lead_dist = math.hypot(hx, hz)
        start, stop = STRIDE_START_M, STRIDE_STOP_M
        g = self._plan_g  # normalised body rows right, up, at (turned this frame)
        aim = [hx * g[i] + hz * g[6 + i] for i in range(3)]  # where to push, world space
        if self._striding:
            # The stride ends once the body is there - or once its momentum will carry it there (the aim points
            # back the way it came). It never pushes back: at the minimum force that rocked the body to and fro.
            back = self._stride_dir is not None and sum(aim[i] * self._stride_dir[i] for i in range(3)) <= 0.0
            self._striding = dist >= stop and lead_dist >= stop and not back
        elif dist >= start and lead_dist > 1e-6:
            self._striding = True
            self._stride_dir = [v / lead_dist for v in aim]
        if not self._striding or lead_dist < 1e-6:
            return
        dist = lead_dist
        ux, uz = hx / dist, hz / dist
        if force:
            mag = min(FORCE_MAX, max(FORCE_MIN, dist * FORCE_GAIN))
            self._force_cmd = [(ux * g[i] + uz * g[6 + i]) * mag for i in range(3)]
            self._walking_until = now + WALK_SETTLE_S
            return
        walk_map = getattr(self.state, "walk_map", None) if WALK_ADAPT_RATE else None
        if walk_map is None or abs(walk_map[0] * walk_map[3] - walk_map[1] * walk_map[2]) < 0.2:
            walk_map = self.state.walk_map = WALK_MAP_DEFAULT  # unset, or an adapted map that went degenerate
        mag = min(WALK_MAX_STICK, max(WALK_MIN_STICK, dist * WALK_GAIN))
        # walk_map is S: stick (x, y) -> body-frame direction (right, at); invert it for the command.
        s00, s01, s10, s11 = walk_map
        det = s00 * s11 - s01 * s10
        if abs(det) < 0.2:
            return
        sx = (s11 * ux - s01 * uz) / det * mag
        sy = (-s10 * ux + s00 * uz) / det * mag
        n = max(1.0, math.hypot(sx, sy))
        self._stick_cmd = (sx / n, sy / n)
        self._cmd_dir = (sx / n / mag, sy / n / mag) if mag else None
        self._walking_until = now + WALK_SETTLE_S

    def _adapt_walk_map(self, vx: float, vz: float):
        """The stick's frame may follow the head or a hand (the game's movement-direction setting), so keep
        the mapping current: nudge S so that the last command maps to the direction the body really went."""
        cmd = self._cmd_dir
        speed = math.hypot(vx, vz)
        if cmd is None or speed < 0.5 or getattr(self.state, "walk_map", None) is None:
            return
        ux, uz = vx / speed, vz / speed
        s00, s01, s10, s11 = self.state.walk_map
        cx, cy = cmd
        ex, ez = ux - (s00 * cx + s01 * cy), uz - (s10 * cx + s11 * cy)  # observed - predicted direction
        k = WALK_ADAPT_RATE
        self.state.walk_map = (s00 + k * ex * cx, s01 + k * ex * cy, s10 + k * ez * cx, s11 + k * ez * cy)

    def _after_physics(self, player: int):
        """Post-physics, before the camera: whatever the walk moved the body this frame is taken out of
        the head-reset matrix, so the view stays with your head while the body catches up."""
        now = time.monotonic()
        if not FOLLOW_WALK or self._pos_before is None or not self._hmd or now >= self._walking_until or self._stick_active():
            if self._stick_cmd is not None and self._stick_active():
                self._blocked_frames += 1  # we pushed the stick, yet it looks like you did: no correction
            self._body_vel = (0.0, 0.0)
            return
        pos = struct.unpack("3f", ctypes.string_at(player + PLAYER_POSITION, 12))
        g = struct.unpack("20f", ctypes.string_at(player + PLAYER_GRAPHICS_MATRIX, 80))
        delta = [pos[i] - self._pos_before[i] for i in range(3)]
        self._pos_before = pos
        nr = math.sqrt(sum(v * v for v in g[0:3])) or 1.0
        na = math.sqrt(sum(v * v for v in g[8:11])) or 1.0
        dxb = sum(delta[i] * g[i] for i in range(3)) / nr
        dzb = sum(delta[i] * g[8 + i] for i in range(3)) / na
        dt = now - self._vel_time
        self._vel_time = now
        if 0.001 < dt < 0.1:
            # Physics advances the body in uneven chunks (0-7 cm per frame while walking): smooth the velocity
            # the lead term damps with, or the noise makes the walk twitch.
            k = min(1.0, dt / BODY_VEL_SMOOTH_S)
            bx, bz = self._body_vel
            self._body_vel = (bx + k * (dxb / dt - bx), bz + k * (dzb / dt - bz))
            self._adapt_walk_map(dxb / dt, dzb / dt)
            if now - self._walk_log_at > 0.5 and math.hypot(dxb, dzb) / dt > 0.1:
                self._walk_log_at = now
                f = self._force_cmd
                logger.info("walk: body v(%.2f, %.2f) m/s in its frame, stick %s, force %s (%d written)",
                            dxb / dt, dzb / dt, None if self._stick_cmd is None else tuple(round(v, 2) for v in self._stick_cmd),
                            None if f is None else round(math.sqrt(sum(v * v for v in f)), 3), self._force_writes)
        px_, pz_ = self._predicted
        self._predicted = (0.0, 0.0)
        self._walked += math.hypot(dxb, dzb)
        if VIEW_HOLD and self._hold_ok:
            return  # the anchor keeps the view still; no delta correction on top
        if abs(dxb - px_) + abs(dzb - pz_) < 1e-7:
            return
        row = (ctypes.c_float * 3).from_address(self._hmd + HMD_INVERSE_RESET + 48)
        row[0] -= dxb - px_
        row[2] -= dzb - pz_

    def _apply_follow(self, player: int):
        """Game thread, right before cGcPlayer::Update: carry out the body side of the plan."""
        g = struct.unpack("20f", ctypes.string_at(player + PLAYER_GRAPHICS_MATRIX, 80))
        pos = struct.unpack("3f", ctypes.string_at(player + PLAYER_POSITION, 12))
        nu = math.sqrt(sum(v * v for v in g[4:7])) or 1.0
        up = [g[4 + i] / nu for i in range(3)]
        # Self-check: a step must not move the body along its up axis any more than a normal frame does.
        # (If the controller's capsule offset ever changes, every step would lift the player.)
        if self._prev_frame_pos is not None:
            lift = sum((pos[i] - self._prev_frame_pos[i]) * up[i] for i in range(3))
            j = 0 if self._stepped_last else 2
            self._lift_check[j] += 1
            self._lift_check[j + 1] += lift
        self._prev_frame_pos = pos
        self._stepped_last = False
        now = time.monotonic()
        if now - self._follow_log_at > 2.0:
            self._follow_log_at = now
            a, al, f, fl = self._lift_check
            if self._walked > 0.005 or abs(self._turned) >= 1.0 or self._blocked_frames or self._held_m > 0.005:
                cs = self._cam_stats
                logger.info("body walked %.2f m after you and turned %.0f deg; view anchor absorbed %.2f m (%s, %d camera frames); "
                            "camera moved %.3f m total, max %.4f m/frame; head moved %.3f m, max %.4f m/frame",
                            self._walked, self._turned, self._held_m, "held" if self._held else "following", self._camera_frames,
                            cs[2], cs[0], cs[3], cs[1])
                self._walked, self._turned, self._blocked_frames, self._held_m, self._camera_frames = 0.0, 0.0, 0, 0.0, 0
                self._cam_stats = [0.0, 0.0, 0.0, 0.0]
            if a:
                step_lift, free_lift = al / a, (fl / f if f else 0.0)
                logger.info("body followed you %.2f m and turned %.0f deg (up-motion per frame: %.4f m after a step, %.4f m otherwise)",
                            self._followed, self._turned, step_lift, free_lift)
                self._turned = 0.0
                if a >= 5 and step_lift - free_lift > 0.3:  # a wrong capsule offset lifts ~0.85 m per step
                    logger.error("body follow disabled: a step moves the player %.2f m along its up axis "
                                 "(controller offset changed? game updated?)", step_lift - free_lift)
                    self._move_ready = False
            self._followed = 0.0
            self._lift_check = [0, 0.0, 0, 0.0]
        plan, self._pending = self._pending, None
        if plan is None or not self._move_ready:
            return
        d, new_at = plan
        if new_at is not None:
            vec = (ctypes.c_float * 4).from_address(self._vec_base)
            vec[:] = list(new_at) + [0.0]
            self._set_facing(player, self._vec_base)
            if d is None:
                self._update_graphics(player, 1)
        if d is not None and self._move_player(player, d, up):
            self._stepped_last = True
            self._prev_frame_pos = (pos[0] + d[0], pos[1] + d[1], pos[2] + d[2])

    # --------------------------------------------------------------- per-frame driver ----
    @nms.cGcPlayer.Update.after
    def player_update(self, this: ctypes._Pointer[nms.cGcPlayer], lfStep: float):
        if not self._live():
            return
        st = self.state
        for name, default in SitState.__dataclass_fields__.items():  # back-fill after hot reloads
            if not hasattr(st, name):
                setattr(st, name, default.default)
        now = time.monotonic()
        self._pose_dy = RECENTER_INJECT_M if now < st.inject_until else 0.0
        self._player = get_addressof(this)

        sit = safe_read(self._player + PLAYER_CHARACTER_SIT + SIT_STATE, ctypes.c_int32)
        sit = sit if sit in (0, 1, 2, 3) else 0
        if sit != st.game_sit:
            logger.info("game sit state %d -> %d", st.game_sit, sit)
            st.game_sit = sit
            if sit == 0:
                st.phys_seated, st.settle_pos, st.inject_until = False, None, 0.0
        target = SEAT_BODY_DROP_M if SEAT_BODY_FIX and sit == SEATED else 0.0
        self._body_drop += (target - self._body_drop) * (1.0 - math.exp(-max(lfStep, 0.0) / BODY_BLEND_SECONDS))
        if abs(self._body_drop - target) < 0.002:
            self._body_drop = target

        if now - st.last_sample < 0.25:
            return
        st.last_sample = now
        self._watch_settings()
        if _VR_FUNCS is None and not st.reload_requested and _openvr() is not None:
            # VR came up after we were imported: reload once so the SteamVR hooks get installed.
            st.reload_requested = True
            logger.info("VR is up -> reloading VRSit to install the SteamVR hooks")
            try:
                mod_manager._gui_reload(None, None, (self._mod_name, self.pymhf_gui))
            except Exception:
                logger.exception("self-reload failed")
            return
        st.hmd_pos = hmd_position()
        if st.hmd_pos is None:
            return
        seated = sit in (SITTING, SEATED)
        # Peak-hold the head height = standing height. While seated in-game it never expires.
        y = st.hmd_pos[1]
        if y >= st.head_peak[0] or (not seated and now - st.head_peak[1] > HEAD_PEAK_WINDOW):
            st.head_peak = (y, now)
        if not seated:
            return
        drop = st.head_peak[0] - y
        if st.phys_seated:
            # The character stays seated; standing up physically only pauses the seat-height recentre.
            if drop < STAND_DROP_M:
                st.phys_seated, st.settle_pos = False, None
                logger.info("you stood up (head %.2f m below standing height); recentres while standing stay at standing height", drop)
        elif drop < SIT_DROP_M:
            st.settle_pos = None
        elif st.settle_pos is None or math.dist(st.hmd_pos, st.settle_pos) > SIT_STILL_M:
            if st.settle_pos is None:
                logger.info("head %.2f m below standing height -> sitting down; recentre once still for %.0f s", drop, SIT_RECENTER_DELAY)
            st.settle_pos, st.settle_since = st.hmd_pos, now
        elif now - st.settle_since >= SIT_RECENTER_DELAY:
            st.phys_seated = True
            logger.info("sitting still, head %.2f m below standing height", drop)
            if SEAT_RECENTER:
                self._recentre()

    def _recentre(self):
        if not (self._input_mgr_getter and self._game_recenter):
            logger.warning("automatic recentre unavailable (game code not found) - press recentre yourself")
            return
        # Only called from the game's update (never from __init__: on a cold start the game is still
        # suspended and the getter would construct the input manager too early).
        input_mgr = ctypes.CFUNCTYPE(ctypes.c_void_p)(self._input_mgr_getter)()
        ctypes.CFUNCTYPE(None, ctypes.c_void_p)(self._game_recenter)(input_mgr)
        logger.info("automatic recentre triggered")

    def _seated_and_low(self) -> bool:
        st = self.state
        if st.game_sit not in (SITTING, SEATED):
            return False
        if st.phys_seated or st.hmd_pos is None:
            return st.phys_seated
        return st.head_peak[0] - st.hmd_pos[1] >= SIT_DROP_M

    # ------------------------------------------------------------- the seated VR body ----
    @manual_hook("cGcPlayerThirdPerson::PostPhysicsUpdate", pattern=THIRD_PERSON_POST_PHYSICS_SIG,
                 func_def=FUNCDEF(None, [ctypes.c_void_p, ctypes.c_float]), detour_time="after")
    def after_body_placed(self, this, lfStep):
        if not self._live():
            return
        if not this or not self._player or not self._third_person_ptr \
                or int(this) != safe_read(self._player + self._third_person_ptr, ctypes.c_uint64):
            return  # another character (e.g. a remote player)
        try:
            self._after_physics(self._player)
        except Exception:
            logger.exception("walk correction failed; disabling the follow")
            self._move_ready = False
        # The game has just placed the character model. In VR the seated body ends up too high, so
        # move the local player's body mesh node down along its own up axis.
        d = self._body_drop
        if not d or self._set_node_world is None:
            self._last_body_write = None
            return
        h = safe_read(int(this) + THIRD_PERSON_MESH_NODE, ctypes.c_uint32)
        if not h or (h & 0x3FFFF) in (0, 0x3FFFF):
            return
        node = basic.TkHandle()
        node.lookupInt = h
        a = GetNodeAbsoluteTransMatrix(node)
        last = self._last_body_write
        if last and last[0] == h and abs(a.pos.x - last[1]) + abs(a.pos.y - last[2]) + abs(a.pos.z - last[3]) < 1e-5:
            return  # the game has not re-placed the node since our last write
        m = (ctypes.c_float * 20)(a.right.x, a.right.y, a.right.z, 0, a.up.x, a.up.y, a.up.z, 0,
                                  a.at.x, a.at.y, a.at.z, 0,
                                  a.pos.x - a.up.x * d, a.pos.y - a.up.y * d, a.pos.z - a.up.z * d, 0, 0, 0, 0, 0)
        self._set_node_world(h, ctypes.addressof(m))
        self._last_body_write = (h, m[12], m[13], m[14])

    # --------------------------------------------------- SteamVR hooks (need VR up) ----
    def _shift(self, pose: TrackedDevicePose_t):
        if pose.bPoseIsValid and self._pose_dy:
            pose.mDeviceToAbsoluteTracking.m[1][3] += self._pose_dy

    if _VR_FUNCS:
        @manual_hook("IVRChaperone::ResetZeroPose", offset=_VR_FUNCS["ResetZeroPose"] - _internal.BASE_ADDRESS,
                     func_def=FUNCDEF(None, [ctypes.c_void_p, ctypes.c_int32]), detour_time="after")
        def on_reset_zero_pose(self, this, eOrigin):
            if not self._live():
                return
            self._recentred_at = time.monotonic()
            # The game is recentring right now. While seated (in-game and physically), feed it raised
            # poses for a moment so the vertical reference it captures lands above the head.
            if SEAT_RECENTER and self._seated_and_low():
                self.state.inject_until = time.monotonic() + RECENTER_INJECT_SECONDS
                self._pose_dy = RECENTER_INJECT_M
                logger.info("recentre while seated -> poses raised %.2f m for %.1f s", RECENTER_INJECT_M, RECENTER_INJECT_SECONDS)

        @manual_hook("IVRCompositor::WaitGetPoses", offset=_VR_FUNCS["WaitGetPoses"] - _internal.BASE_ADDRESS,
                     func_def=FUNCDEF(ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                                       ctypes.c_void_p, ctypes.c_uint32]),
                     detour_time="after")
        def on_wait_get_poses(self, this, pRender, nRender, pGame, nGame):
            if not self._live():
                return
            if self._pose_dy:
                for ptr, n in ((pRender, nRender), (pGame, nGame)):
                    if ptr and n:
                        arr = (TrackedDevicePose_t * min(int(n), 16)).from_address(ptr)
                        for pose in arr:
                            self._shift(pose)
            # (Planning the follow here, on the pose job, made the view flip during turns: the renderer reads
            # the inverse reset matrix on its own schedule, before the body had turned. Game thread only.)

        @manual_hook("IVRInput::UpdateActionState",
                     offset=_VR_FUNCS["UpdateActionState"] - _internal.BASE_ADDRESS,
                     func_def=FUNCDEF(ctypes.c_int32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]),
                     detour_time="before")
        def on_update_action_state(self, this, pSets, size, count):
            if not self._live() or not pSets:
                return
            # The action sets the game wants this frame tell what you are doing: a menu set means a menu is open.
            stride = int(size) if size else 32  # VRActiveActionSet_t: set, restricted device, secondary set, ...
            names = set()
            for k in range(min(int(count), 32)):
                base = int(pSets) + k * stride
                for off in (0, 16):  # ulActionSet, ulSecondaryActionSet
                    h = ctypes.c_uint64.from_address(base + off).value
                    if h:
                        names.add(_ACTION_SETS.get(h, hex(h)))
            names = tuple(sorted(names))
            self._menu_active = any(n.startswith(MENU_ACTION_SETS) for n in names)
            self._other_menu_active = any(n.startswith(OTHER_MENU_ACTION_SETS) for n in names)
            self._menu_seen_at = time.monotonic()
            if names != self._action_sets:
                self._action_sets = names
                if self._action_set_logs < 200:
                    self._action_set_logs += 1
                    logger.info("SteamVR action sets: %s%s", ", ".join(n.replace("/actions/", "") for n in names),
                                " -> menu" if self._menu_active or self._other_menu_active else "")

        @manual_hook("IVRInput::GetAnalogActionData",
                     offset=_VR_FUNCS["GetAnalogActionData"] - _internal.BASE_ADDRESS,
                     func_def=FUNCDEF(ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
                                                       ctypes.c_uint32, ctypes.c_uint64]),
                     detour_time="after")
        def on_analog_action(self, this, action, pActionData, size, restrict):
            if not self._live():
                return
            # The on-foot Move stick. Remember whether you are using it; when not, the follow may walk the
            # body for you by writing its own stick vector here.
            if not pActionData or action != _VR_FUNCS.get("MoveAction"):
                return
            data = InputAnalogActionData_t.from_address(pActionData)
            raw = (data.x, data.y) if data.bActive else (0.0, 0.0)
            last = self._stick_written
            if last is not None and abs(raw[0] - last[0]) < 1e-4 and abs(raw[1] - last[1]) < 1e-4:
                raw = (0.0, 0.0)  # our own push read back (the game asks more than once a frame), not you
            self._stick_user = raw
            self._stick_time = time.monotonic()
            if math.hypot(raw[0], raw[1]) > STICK_DEADZONE:
                self._stick_active_at = self._stick_time
            cmd = self._stick_cmd
            if cmd is not None and abs(raw[0]) < 0.1 and abs(raw[1]) < 0.1:
                data.bActive = True
                data.x, data.y = cmd
                self._stick_written = (data.x, data.y)
            else:
                self._stick_written = None

        @manual_hook("IVRInput::GetPoseActionDataRelativeToNow",
                     offset=_VR_FUNCS["GetPoseActionDataRelativeToNow"] - _internal.BASE_ADDRESS,
                     func_def=FUNCDEF(ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int32, ctypes.c_float,
                                                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64]),
                     detour_time="after")
        def on_pose_action_now(self, this, action, eOrigin, fPredicted, pActionData, size, restrict):
            if not self._live():
                return
            if pActionData and self._pose_dy:
                data = InputPoseActionData_t.from_address(pActionData)
                if data.bActive:
                    self._shift(data.pose)

        @manual_hook("IVRInput::GetPoseActionDataForNextFrame",
                     offset=_VR_FUNCS["GetPoseActionDataForNextFrame"] - _internal.BASE_ADDRESS,
                     func_def=FUNCDEF(ctypes.c_int32, [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int32,
                                                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64]),
                     detour_time="after")
        def on_pose_action_next(self, this, action, eOrigin, pActionData, size, restrict):
            if not self._live():
                return
            if pActionData and self._pose_dy:
                data = InputPoseActionData_t.from_address(pActionData)
                if data.bActive:
                    self._shift(data.pose)


# --------------------------------------------------------------- settings in the GUI ----
def _add_setting_widgets(cls) -> None:
    """One field per SETTINGS entry in the mod's tab of the pyMHF window, grouped as listed."""
    for group in dict.fromkeys(g for g, *_rest in SETTINGS):
        with gui_group(group):
            for g, name, kind, label, lo, hi, step in SETTINGS:
                if g != group:
                    continue
                attr = "setting_" + name.lower()

                def fget(self, _name=name):
                    return globals()[_name]

                def fset(self, value, _name=name):
                    self._set_setting(_name, value)

                fget.__name__ = attr  # the GUI writes back with setattr(mod, fget.__name__, value)
                fget.__qualname__ = f"{cls.__name__}.{attr}"
                if kind is bool:
                    widget = BOOLEAN(label)
                else:
                    widget = FLOAT(label, min_value=lo, max_value=hi, min_clamped=True, max_clamped=True,
                                   step=step, format="%.0f" if step >= 1 else "%.2f")
                setattr(cls, attr, property(widget(fget), fset))

    def reset_settings(self):
        for name, value in SETTING_DEFAULTS.items():
            globals()[name] = value
        global _settings_mtime
        _settings_mtime = save_settings()
        logger.info("settings reset to the defaults")
        self._apply_settings()

    reset_settings.__qualname__ = f"{cls.__name__}.reset_settings"
    with gui_group("Settings file"):
        cls.reset_settings = gui_button("Reset all to defaults")(reset_settings)


_add_setting_widgets(VRSit)
