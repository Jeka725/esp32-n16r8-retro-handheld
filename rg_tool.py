#!/usr/bin/env python3
import argparse
import subprocess
import shutil
import glob
import math
import sys
import re
import os
import struct

DEFAULT_TARGET = os.getenv("RG_TOOL_TARGET", "odroid-go")
DEFAULT_BAUD = os.getenv("RG_TOOL_BAUD", "1152000")
DEFAULT_PORT = os.getenv("RG_TOOL_PORT", "COM3")
DEFAULT_APPS = os.getenv("RG_TOOL_APPS", "launcher retro-core prboom-go gwenesis fmsx")
PROJECT_NAME = os.getenv("PROJECT_NAME", "Retro-Go")
PROJECT_ICON = os.getenv("PROJECT_ICON", "assets/icon.raw")
PROJECT_APPS = {
  # Project name  Type, SubType, Size
  'launcher':     [0, 0, 983040],
  'retro-core':   [0, 0, 983040],
  'prboom-go':    [0, 0, 851968],
  'gwenesis':     [0, 0, 983040],
  'fmsx':         [0, 0, 589824],
  'gbsp':         [0, 0, 851968],
}

try:
    PROJECT_VER = os.getenv("PROJECT_VER") or subprocess.check_output(
        "git describe --tags --abbrev=5 --dirty --always", shell=True
    ).decode().rstrip()
except:
    PROJECT_VER = "unknown"
FW_FORMAT = "none"

TARGETS = []
for t in glob.glob("components/retro-go/targets/*/config.h"):
    TARGETS.append(os.path.basename(os.path.dirname(t)))

IDF_TARGET = os.getenv("IDF_TARGET", "esp32")
IDF_PATH = os.getenv("IDF_PATH")
if not IDF_PATH:
    exit("IDF_PATH is not defined. Are you running inside esp-idf environment?")

if os.name == 'nt':
    IDF_PY = os.path.join(IDF_PATH, "tools", "idf.py")
    IDF_MONITOR_PY = os.path.join(IDF_PATH, "tools", "idf_monitor.py")
    ESPTOOL_PY = os.path.join(IDF_PATH, "components", "esptool_py", "esptool", "esptool.py")
    PARTTOOL_PY = os.path.join(IDF_PATH, "components", "partition_table", "parttool.py")
    GEN_ESP32PART_PY = os.path.join(IDF_PATH, "components", "partition_table", "gen_esp32part.py")
else:
    IDF_PY = "idf.py"
    IDF_MONITOR_PY = "idf_monitor.py"
    ESPTOOL_PY = "esptool.py"
    PARTTOOL_PY = "parttool.py"
    GEN_ESP32PART_PY = "gen_esp32part.py"
MKFW_PY = os.path.join("tools", "mkfw.py")


def run(cmd, cwd=None, check=True):
    print(f"Running command: {' '.join(cmd)}")
    if os.name == 'nt' and cmd[0].endswith(".py"):
        return subprocess.run(["python", *cmd], shell=True, cwd=cwd, check=check)
    return subprocess.run(cmd, shell=False, cwd=cwd, check=check)


def build_firmware(output_file, apps, fw_format="odroid-go", fatsize=0):
    print("Building firmware with: %s\n" % " ".join(apps))
    args = [MKFW_PY, output_file, f"{PROJECT_NAME} {PROJECT_VER}", PROJECT_ICON]

    if fw_format == "esplay":
        args.append("--esplay")

    for app in apps:
        part = PROJECT_APPS[app]
        args += [str(part[0]), str(part[1]), str(part[2]), app, os.path.join(app, "build", app + ".bin")]

    run(args)


def parse_size(value):
    """Parse sizes such as 8M, 10M, 512K or plain bytes."""
    if value is None or value == 0:
        return 0
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMG]?)B?\s*", str(value), re.IGNORECASE)
    if not match:
        raise ValueError("Invalid size: %s" % value)
    number = float(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[unit]
    return int(number * multiplier)


def _fat16_short_name(long_name, used_names):
    """Create a unique 8.3 alias for a root-directory FAT16 file."""
    stem, ext = os.path.splitext(os.path.basename(long_name))
    stem = "".join(ch for ch in stem.upper() if ch.isalnum())
    ext = "".join(ch for ch in ext[1:].upper() if ch.isalnum())[:3]
    base = (stem[:8] or "FILE")
    candidate = (base.ljust(8) + ext.ljust(3)).encode("ascii")
    if candidate not in used_names:
        return candidate
    for n in range(1, 100):
        tail = "~%d" % n
        base_n = (stem[:8 - len(tail)] + tail)[:8]
        candidate = (base_n.ljust(8) + ext.ljust(3)).encode("ascii")
        if candidate not in used_names:
            return candidate
    raise RuntimeError("Unable to create unique FAT 8.3 alias for %s" % long_name)


def _fat16_lfn_entries(long_name, short_name):
    """Return LFN directory entries for a FAT16 root directory."""
    encoded = long_name.encode("utf-16le")
    code_units = list(struct.unpack("<%dH" % (len(encoded) // 2), encoded))
    checksum = 0
    for value in short_name:
        checksum = ((checksum & 1) << 7) + (checksum >> 1) + value
        checksum &= 0xFF

    chunks = [code_units[i:i + 13] for i in range(0, len(code_units), 13)]
    entries = []
    for index in range(len(chunks) - 1, -1, -1):
        units = chunks[index] + [0x0000]
        units += [0xFFFF] * (13 - len(units))
        ordinal = index + 1
        if index == len(chunks) - 1:
            ordinal |= 0x40

        entry = bytearray(32)
        entry[0] = ordinal
        struct.pack_into("<5H", entry, 1, *units[0:5])
        entry[11] = 0x0F
        entry[12] = 0
        entry[13] = checksum
        struct.pack_into("<6H", entry, 14, *units[5:11])
        struct.pack_into("<2H", entry, 28, *units[11:13])
        entries.append(bytes(entry))
    return entries


def _build_fat16_image(output_file, file_paths, image_size):
    """Build a self-contained FAT16 image using only Python stdlib."""
    sector_size = 512
    if image_size % sector_size:
        raise RuntimeError("FAT16 image size must be sector-aligned")
    total_sectors = image_size // sector_size
    if total_sectors < 4096:
        raise RuntimeError("FAT16 image is too small")

    reserved_sectors = 1
    fat_count = 2
    root_entries = 512
    root_dir_sectors = (root_entries * 32 + sector_size - 1) // sector_size
    sectors_per_cluster = 1

    fat_sectors = 1
    for _ in range(16):
        data_sectors = total_sectors - reserved_sectors - fat_count * fat_sectors - root_dir_sectors
        cluster_count = data_sectors // sectors_per_cluster
        needed = ((cluster_count + 2) * 2 + sector_size - 1) // sector_size
        if needed == fat_sectors:
            break
        fat_sectors = needed

    if cluster_count < 4085 or cluster_count > 65524:
        raise RuntimeError("Configured image does not produce a valid FAT16 cluster count")

    image = bytearray(b"\xFF" * image_size)

    # FAT16 BIOS Parameter Block.
    image[0:3] = b"\xEB\x3C\x90"
    image[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", image, 11, sector_size)
    image[13] = sectors_per_cluster
    struct.pack_into("<H", image, 14, reserved_sectors)
    image[16] = fat_count
    struct.pack_into("<H", image, 17, root_entries)
    struct.pack_into("<H", image, 19, total_sectors)
    image[21] = 0xF8
    struct.pack_into("<H", image, 22, fat_sectors)
    struct.pack_into("<H", image, 24, 32)
    struct.pack_into("<H", image, 26, 64)
    struct.pack_into("<I", image, 28, 0)
    struct.pack_into("<I", image, 32, 0)
    image[36] = 0x80
    image[38] = 0x29
    struct.pack_into("<I", image, 39, 0x5254474F)
    image[43:54] = b"RETROGO    "
    image[54:62] = b"FAT16   "
    image[510:512] = b"\x55\xAA"

    fat_offset = reserved_sectors * sector_size
    fat_bytes = fat_sectors * sector_size
    fat = bytearray(fat_bytes)
    struct.pack_into("<H", fat, 0, 0xFFF8)
    struct.pack_into("<H", fat, 2, 0xFFFF)

    root_offset = (reserved_sectors + fat_count * fat_sectors) * sector_size
    data_offset = root_offset + root_dir_sectors * sector_size
    root = bytearray(root_dir_sectors * sector_size)

    used_short_names = set()
    root_pos = 0
    next_cluster = 2

    for path in file_paths:
        name = os.path.basename(path)
        size = os.path.getsize(path)
        clusters_needed = max(1, (size + sector_size - 1) // sector_size)
        if next_cluster + clusters_needed - 2 > cluster_count:
            raise RuntimeError("FAT16 image is too small for bundled files")

        short_name = _fat16_short_name(name, used_short_names)
        used_short_names.add(short_name)
        lfn_entries = _fat16_lfn_entries(name, short_name)
        entry_count = len(lfn_entries) + 1
        if root_pos + entry_count * 32 > len(root) - 32:
            raise RuntimeError("FAT16 root directory is full")

        first_cluster = next_cluster
        for cluster in range(first_cluster, first_cluster + clusters_needed):
            next_value = 0xFFFF if cluster == first_cluster + clusters_needed - 1 else cluster + 1
            struct.pack_into("<H", fat, cluster * 2, next_value)

        with open(path, "rb") as src:
            remaining = size
            cluster = first_cluster
            while remaining:
                chunk = src.read(sector_size)
                if not chunk:
                    raise RuntimeError("Unexpected EOF while packaging %s" % name)
                offset = data_offset + (cluster - 2) * sector_size
                image[offset:offset + len(chunk)] = chunk
                remaining -= len(chunk)
                cluster += 1

        for entry in lfn_entries:
            root[root_pos:root_pos + 32] = entry
            root_pos += 32

        entry = bytearray(32)
        entry[0:11] = short_name
        entry[11] = 0x20
        struct.pack_into("<H", entry, 26, first_cluster)
        struct.pack_into("<I", entry, 28, size)
        root[root_pos:root_pos + 32] = entry
        root_pos += 32
        next_cluster += clusters_needed

    image[fat_offset:fat_offset + fat_bytes] = fat
    second_fat_offset = fat_offset + fat_bytes
    image[second_fat_offset:second_fat_offset + fat_bytes] = fat
    image[root_offset:root_offset + len(root)] = root

    with open(output_file, "wb") as dst:
        dst.write(image)



def build_image(output_file, apps, img_format="esp32", fatsize=0):
    print("Building image with: %s\n" % " ".join(apps))
    image_data = bytearray(b"\xFF" * 0x10000)
    table_ota = 0
    table_csv = [
        "nvs, data, nvs, 36864, 16384",
        "otadata, data, ota, 53248, 8192",
        "phy_init, data, phy, 61440, 4096",
    ]

    for app in apps:
        with open(os.path.join(app, "build", app + ".bin"), "rb") as f:
            data = f.read()
        part_size = max(PROJECT_APPS[app][2], math.ceil(len(data) / 0x10000) * 0x10000)
        table_csv.append("%s, app, ota_%d, %d, %d" % (app, table_ota, len(image_data), part_size))
        table_ota += 1
        image_data += data + b"\xFF" * (part_size - len(data))

    if fatsize:
        # The VFS partition is real FAT on the ESP32-S3's internal SPI flash.
        # Package the two supported ROM files directly from the repository root.
        # ZIP archives and other root files are intentionally not copied.
        fat_size = parse_size(fatsize)
        fat_dir = os.path.abspath(".")
        rom_files = [
            "Sonic The Hedgehog (USA, Europe).md",
            "Super Mario Advance (USA, Europe).gba",
        ]
        missing = [name for name in rom_files if not os.path.isfile(os.path.join(fat_dir, name))]
        if missing:
            raise RuntimeError("Required root ROM file(s) missing: %s" % ", ".join(missing))

        fat_image = os.path.abspath("storage_fat.bin")
        # Build a raw FAT16 image with the Python standard library.
        # This is mounted read-only by ESP-IDF 4.4 and avoids host-tool dependencies.
        rom_paths = [os.path.join(fat_dir, name) for name in rom_files]
        _build_fat16_image(fat_image, rom_paths, fat_size)
        with open(fat_image, "rb") as f:
            fat_data = f.read()
        table_csv.append("vfs, data, fat, %d, %d" % (len(image_data), fat_size))
        image_data += fat_data

    print("Generating partition table...")
    with open("partitions.csv", "w") as f:
        f.write("\n".join(table_csv))
    run([GEN_ESP32PART_PY, "partitions.csv", "partitions.bin"])
    with open("partitions.bin", "rb") as f:
        table_bin = f.read()

    print("Building bootloader...")
    bootloader_file = os.path.join(os.getcwd(), list(apps)[0], "build", "bootloader", "bootloader.bin")
    if not os.path.exists(bootloader_file):
        run([IDF_PY, "bootloader"], cwd=os.path.join(os.getcwd(), list(apps)[0]))
    with open(bootloader_file, "rb") as f:
        bootloader_bin = f.read()

    if img_format == "esp32s3":
        image_data[0x0000:0x0000+len(bootloader_bin)] = bootloader_bin
        image_data[0x8000:0x8000+len(table_bin)] = table_bin
    elif img_format == "esp32p4":
        image_data[0x2000:0x2000+len(bootloader_bin)] = bootloader_bin
        image_data[0x8000:0x8000+len(table_bin)] = table_bin
    else:
        image_data[0x1000:0x1000+len(bootloader_bin)] = bootloader_bin
        image_data[0x8000:0x8000+len(table_bin)] = table_bin

    with open(output_file, "wb") as f:
        f.write(image_data)

    print("\nPartition table:")
    print("\n".join(table_csv))
    print("\nSaved image '%s' (%d bytes)\n" % (output_file, len(image_data)))


def clean_app(app):
    print("Cleaning up app '%s'..." % app)
    try:
        os.unlink(os.path.join(app, "sdkconfig"))
        os.unlink(os.path.join(app, "sdkconfig.old"))
    except:
        pass
    try:
        shutil.rmtree(os.path.join(app, "build"))
    except:
        pass
    print("Done.\n")


def build_app(app, device_type, with_profiling=False, no_networking=False, is_release=False):
    print("Building app '%s'" % app)
    args = [IDF_PY, "app"]
    args.append(f"-DRG_PROJECT_APP={app}")
    args.append(f"-DRG_PROJECT_VER={PROJECT_VER}")
    args.append(f"-DRG_BUILD_TARGET=RG_TARGET_{re.sub(r'[^A-Z0-9]', '_', device_type.upper())}")
    args.append(f"-DRG_BUILD_RELEASE={1 if is_release else 1}")
    args.append(f"-DRG_ENABLE_PROFILING={1 if with_profiling else 0}")
    args.append(f"-DRG_ENABLE_NETWORKING={0 if no_networking else 1}")
    with open("partitions.csv", "w") as f:
        f.write("# This table isn't used, it's just needed to avoid esp-idf build failures.\n")
        f.write("dummy, app, ota_0, 65536, 3145728\n")
    run(args, cwd=os.path.join(os.getcwd(), app))
    print("Done.\n")


def flash_app(app, port, baudrate=1152000):
    os.putenv("ESPTOOL_CHIP", IDF_TARGET)
    os.putenv("ESPTOOL_BAUD", str(baudrate))
    os.putenv("ESPTOOL_PORT", port)
    if not os.path.exists("partitions.bin"):
        print("Reading device's partition table...")
        run([ESPTOOL_PY, "read_flash", "0x8000", "0x1000", "partitions.bin"], check=False)
        run([GEN_ESP32PART_PY, "partitions.bin"], check=False)
    app_bin = os.path.join(app, "build", app + ".bin")
    print(f"Flashing '{app_bin}' to port {port}")
    run([PARTTOOL_PY, "--partition-table-file", "partitions.bin", "write_partition", "--partition-name", app, "--input", app_bin])
    print("Done.\n")


def flash_image(image_file, port, baudrate=1152000):
    os.putenv("ESPTOOL_CHIP", IDF_TARGET)
    os.putenv("ESPTOOL_BAUD", str(baudrate))
    os.putenv("ESPTOOL_PORT", port)
    print(f"Flashing image file '{image_file}' to {port}")
    run([ESPTOOL_PY, "write_flash", "--flash_size", "detect", "0x0", image_file])
    print("Done.\n")


def monitor_app(app, port, baudrate=115200):
    print(f"Starting monitor for app {app} on port {port}")
    elf_file = os.path.join(os.getcwd(), app, "build", app + ".elf")
    if os.path.exists(elf_file):
        run([IDF_MONITOR_PY, "--port", port, elf_file])
    else:
        run([IDF_MONITOR_PY, "--port", port, "-d", sys.argv[0]])


parser = argparse.ArgumentParser(description="Retro-Go build tool")
parser.add_argument("command", choices=["build-fw", "build-img", "release", "build", "clean", "flash", "monitor", "run", "profile", "install"])
parser.add_argument("apps", nargs="*", default="all", choices=["all"] + list(PROJECT_APPS.keys()))
parser.add_argument("--target", default=DEFAULT_TARGET, choices=set(TARGETS), help="Device to target")
parser.add_argument("--no-networking", action="store_const", const=True, help="Build without networking support")
parser.add_argument("--port", default=DEFAULT_PORT, help="Serial port to use for flash and monitor")
parser.add_argument("--baud", default=DEFAULT_BAUD, help="Serial baudrate to use for flashing")
parser.add_argument("--fatsize", help="Add FAT storage partition of provided size (500K, 5M,...) to the built image.")
args = parser.parse_args()

if os.path.exists(f"components/retro-go/targets/{args.target}/env.py"):
    with open(f"components/retro-go/targets/{args.target}/env.py", "rb") as f:
        prev_idf_target = os.getenv("IDF_TARGET")
        exec(f.read())
        if os.getenv("IDF_TARGET") != prev_idf_target:
            IDF_TARGET = os.getenv("IDF_TARGET")

if os.path.exists(f"components/retro-go/targets/{args.target}/sdkconfig"):
    os.putenv("SDKCONFIG_DEFAULTS", os.path.abspath(f"components/retro-go/targets/{args.target}/sdkconfig"))
os.putenv("IDF_TARGET", IDF_TARGET)

command = args.command
apps = DEFAULT_APPS.split() if "all" in args.apps else args.apps
apps = [app for app in PROJECT_APPS.keys() if app in apps]

try:
    if command in ["build-fw", "build-img", "release", "install"] and "launcher" not in apps:
        print("\nWARNING: The launcher is mandatory for those apps and will be included!\n")
        apps.insert(0, "launcher")

    if command in ["clean", "release"]:
        print("=== Step: Cleaning ===\n")
        for app in apps:
            clean_app(app)

    if command in ["build", "build-fw", "build-img", "release", "run", "profile", "install"]:
        print("=== Step: Building ===\n")
        for app in apps:
            build_app(app, args.target, command == "profile", args.no_networking, command == "release")

    if command in ["build-fw", "release"]:
        print("=== Step: Packing ===\n")
        if FW_FORMAT in ["odroid", "esplay"]:
            fw_file = ("%s_%s_%s.fw" % (PROJECT_NAME, PROJECT_VER, args.target)).lower()
            build_firmware(fw_file, apps, FW_FORMAT, args.fatsize)
        else:
            print("Device doesn't support fw format, try build-img!")

    if command in ["build-img", "release", "install"]:
        print("=== Step: Packing ===\n")
        img_file = ("%s_%s_%s.img" % (PROJECT_NAME, PROJECT_VER, args.target)).lower()
        build_image(img_file, apps, IDF_TARGET, args.fatsize)

    if command in ["install"]:
        print("=== Step: Flashing entire image to device ===\n")
        img_file = ("%s_%s_%s.img" % (PROJECT_NAME, PROJECT_VER, args.target)).lower()
        flash_image(img_file, args.port, args.baud)

    if command in ["flash", "run", "profile"]:
        print("=== Step: Flashing ===\n")
        try: os.unlink("partitions.bin")
        except: pass
        for app in apps:
            flash_app(app, args.port, args.baud)

    if command in ["monitor", "run", "profile"]:
        print("=== Step: Monitoring ===\n")
        monitor_app(apps[0] if len(apps) else "none", args.port)

    print("All done!")

except KeyboardInterrupt as e:
    exit("\n")

except Exception as e:
    exit(f"\nTask failed: {e}")
