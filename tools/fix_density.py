# -*- coding: utf-8 -*-
"""Pin shennong native display density 560 in product/etc/build.prop.

Cause:
  The REAL source of the too-small UI is product/etc/build.prop, which ships
      persist.miui.density_v2=480
      ro.sf.lcd_density=480
  480 is the donor (madrid) density; shennong (Xiaomi 14 Pro) is 1440x3200
  @560dpi. init loads build.prop files by writing properties directly
  (bypassing property_service's ro.* set-once check), and product props are
  loaded AFTER vendor ones, so any earlier vendor-side value is overwritten.

Change: patch the two lines in product/etc/build.prop and add an explicit
vendor.display.lcd_density=560. Note persist.miui.density_v2 is a persistent
property: on a dirty flash the value in /data/property can take precedence
again. Since ro.sf.lcd_density is set-once from build.prop, MIUI's later
attempts to re-apply the stale 480 are rejected, so the UI still renders at
the native 560. For a fully clean state, also delete
/data/property/persist.miui.density_v2 (or format data).

CLI:  python fix_density.py <product_tree_dir>
API:  apply_to_tree(product_dir) -> list[str]
"""
import sys
from pathlib import Path

DENSITY = "560"
BP_REL = Path("etc") / "build.prop"

OLD_LINES = (
    "persist.miui.density_v2=480\n",
    "ro.sf.lcd_density=480\n",
)
NEW_LINES = (
    "# XMAPort: 480 is the donor (madrid) density. shennong is 1440x3200\n"
    "# @560dpi; these values are loaded by init AFTER vendor props and win.\n"
    "persist.miui.density_v2={D}\n"
    "ro.sf.lcd_density={D}\n"
    "vendor.display.lcd_density={D}\n"
).format(D=DENSITY)


def _read(p):
    with open(p, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def _write(p, s):
    data = s.encode("utf-8", "surrogateescape")
    assert b"\r" not in data.replace(b"\r\n", b""), "stray CR would corrupt build.prop"
    with open(p, "wb") as f:
        f.write(data)


def apply_to_tree(product_dir, config_dir=None):
    """Patch an unpacked product tree in place. Returns log lines."""
    out = []
    bp = Path(product_dir) / BP_REL
    if not bp.is_file():
        out.append("SKIP: {}/{} not found (not a product tree?)".format(
            product_dir, BP_REL.as_posix()))
        return out
    text = _read(bp)
    # product/etc/build.prop on this port is CRLF while vendor's is LF.
    # Preserve whatever the file uses; mixing breaks init's prop parser.
    nl = "\r\n" if "\r\n" in text else "\n"
    old_lines = tuple(l.replace("\n", nl) for l in OLD_LINES)
    new_lines = NEW_LINES.replace("\n", nl)
    if "ro.sf.lcd_density={}".format(DENSITY) in text and \
       "persist.miui.density_v2={}".format(DENSITY) in text:
        out.append("SKIP: already patched (density == {})".format(DENSITY))
        return out
    missing = [l.strip() for l in old_lines if l not in text]
    if missing:
        out.append("WARN: expected 480 lines not found: {} -- layout changed? "
                   "density fix skipped".format(", ".join(missing)))
        return out
    text = text.replace(old_lines[0], "", 1)
    text = text.replace(old_lines[1], new_lines, 1)
    _write(bp, text)
    out.append("OK: product/etc/build.prop pinned ro.sf.lcd_density={} / "
               "persist.miui.density_v2={} / vendor.display.lcd_density={} "
               "(newlines={})"
               .format(DENSITY, DENSITY, DENSITY,
                       "CRLF" if nl == "\r\n" else "LF"))
    return out


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    lines = apply_to_tree(sys.argv[1])
    for line in lines:
        print(line)
    return 1 if any(l.startswith(("WARN", "SKIP")) for l in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
