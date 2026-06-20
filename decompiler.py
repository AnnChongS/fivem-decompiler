"""
FiveM FXAP Decompiler — Core logic (v3)
Resources must be in citizen/system_resources/ for this FXServer version.
Uses /proc/PID/mem scanning to find decrypted Lua bytecode.
"""

import os
import sys
import time
import shutil
import signal
import zipfile
import subprocess
import tempfile
import re
import struct
from pathlib import Path

BASE_DIR = Path(__file__).parent
FXSERVER_DIR = Path("/opt/fxserver")
SERVER_DATA = Path("/opt/fxserver/server-data")
CITIZEN_RESOURCES = Path("/opt/fxserver/alpine/opt/cfx-server/citizen/system_resources")
UNLUAC_JAR = BASE_DIR / "unluac.jar"
RUN_SH = FXSERVER_DIR / "run.sh"


def setup_stubs():
    """Create stub resources for oxmysql and ox_lib if missing"""
    stubs = {
        "ox_lib": {
            "fxmanifest.lua": "fx_version 'cerulean'\ngame 'gta5'\nname 'ox_lib'\nversion '0.0.0'\n",
            "init.lua": "-- stub\n"
        },
        "oxmysql": {
            "fxmanifest.lua": "fx_version 'cerulean'\ngame 'gta5'\nname 'oxmysql'\nversion '0.0.0'\nserver_script 'lib/MySQL.lua'\n",
            "lib/MySQL.lua": "MySQL = {}\nfunction MySQL.query() return {} end\nfunction MySQL.single() return {} end\nfunction MySQL.scalar() return 0 end\nfunction MySQL.insert() return 0 end\nfunction MySQL.update() return 0 end\n"
        }
    }
    for name, files in stubs.items():
        dest = CITIZEN_RESOURCES / name
        if not dest.exists():
            for rel_path, content in files.items():
                fpath = dest / rel_path
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content)


def ensure_system_resources():
    """Copy system resources from server-data to citizen/system_resources if missing"""
    src = SERVER_DATA / "resources"
    for d in ["sessionmanager", "hardcap", "mapmanager", "spawnmanager", "chat"]:
        dest = CITIZEN_RESOURCES / d
        src_dir = src / d
        if src_dir.exists() and not dest.exists():
            shutil.copytree(src_dir, dest)


def prepare_server(license_key: str, resource_zip: str, extract_dir: str):
    """Extract resource and place it in citizen/system_resources"""
    resource_name = None
    with zipfile.ZipFile(resource_zip, 'r') as zf:
        zf.extractall(extract_dir)

    for root, dirs, files in os.walk(extract_dir):
        if 'fxmanifest.lua' in files or '__resource.lua' in files:
            resource_name = os.path.basename(root)
            resource_path = root
            break

    if not resource_name:
        raise RuntimeError("No fxmanifest.lua or __resource.lua found in zip")

    # Copy to citizen/system_resources (NOT server-data/resources)
    dest = CITIZEN_RESOURCES / resource_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(resource_path, dest)

    # Write server.cfg
    cfg_path = SERVER_DATA / "server.cfg"
    cfg_content = f'''sv_maxclients 1
endpoint_add_tcp "0.0.0.0:30120"
endpoint_add_udp "0.0.0.0:30120"
set sv_licenseKey "{license_key}"

ensure sessionmanager
ensure hardcap
ensure {resource_name}
'''
    cfg_path.write_text(cfg_content)

    # Also write to alpine path for proot
    alpine_cfg = Path("/opt/fxserver/alpine/opt/cfx-server/server-data/server.cfg")
    if alpine_cfg.parent.exists():
        alpine_cfg.write_text(cfg_content)

    return resource_name


def find_fxserver_pid():
    """Find the real FXServer PID (not proot)"""
    for _ in range(15):
        try:
            result = subprocess.run(
                ["pgrep", "-a", "FXServer"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                pid_str = parts[0]
                cmdline = ' '.join(parts[1:])
                if 'FXServer' in cmdline and '-dumpserver' not in cmdline:
                    if pid_str.isdigit():
                        return int(pid_str)
        except:
            pass
        time.sleep(2)
    return None


def scan_memory_for_lua(pid: int, dump_dir: str, timeout: int = 30):
    """Scan /proc/PID/mem for Lua bytecode (\\x1bLua)"""
    dumped = []
    seen_offsets = set()
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            if not os.path.exists(f"/proc/{pid}/maps"):
                break

            with open(f"/proc/{pid}/maps", 'r') as maps_f, \
                 open(f"/proc/{pid}/mem", 'rb') as mem_f:
                for line in maps_f:
                    parts = line.split()
                    if len(parts) < 2 or 'r' not in parts[1]:
                        continue
                    try:
                        addr_range = parts[0].split('-')
                        start = int(addr_range[0], 16)
                        end = int(addr_range[1], 16)
                        if end - start > 100 * 1024 * 1024:
                            continue
                        region_key = (start, end)
                        if region_key in seen_offsets:
                            continue
                        seen_offsets.add(region_key)

                        mem_f.seek(start)
                        data = mem_f.read(end - start)

                        magic = b'\x1bLua'
                        idx = 0
                        while True:
                            idx = data.find(magic, idx)
                            if idx == -1:
                                break
                            abs_offset = start + idx
                            chunk = data[idx:idx + 65536]
                            if len(chunk) >= 32 and chunk[4] == 0x54:  # Lua 5.4
                                fname = f"lua_{abs_offset:016x}.bin"
                                fpath = os.path.join(dump_dir, fname)
                                if not os.path.exists(fpath):
                                    with open(fpath, 'wb') as df:
                                        df.write(chunk)
                                    dumped.append(fpath)
                                    print(f"[DUMP] {fname} ({len(chunk)} bytes)")
                            idx += 1
                    except (OSError, ValueError):
                        continue
        except (OSError, ValueError):
            pass
        time.sleep(2)

    return dumped


def start_fxserver_and_dump(license_key: str, resource_name: str, dump_dir: str, timeout: int = 60):
    """Start FXServer, wait for resource load, scan memory, kill"""

    # Ensure stubs and system resources are in place
    setup_stubs()
    ensure_system_resources()

    log_file = open("/tmp/fivem_decompiler_fxserver.log", "w")
    proc = subprocess.Popen(
        [str(RUN_SH)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(FXSERVER_DIR)
    )

    # Wait for FXServer to load
    time.sleep(8)

    fx_pid = find_fxserver_pid()
    if not fx_pid:
        time.sleep(5)
        fx_pid = find_fxserver_pid()

    if not fx_pid:
        proc.kill()
        raise RuntimeError("Could not find FXServer process")

    print(f"[+] FXServer PID: {fx_pid}")

    # Give it time to load resources
    time.sleep(10)

    # Scan memory
    dumped = scan_memory_for_lua(fx_pid, dump_dir, timeout=timeout)

    # Kill
    try:
        proc.send_signal(signal.SIGINT)
        time.sleep(2)
        if proc.poll() is None:
            proc.kill()
    except:
        pass
    subprocess.run(["pkill", "-9", "FXServer"], capture_output=True)
    subprocess.run(["pkill", "-9", "proot"], capture_output=True)

    return dumped


def decompile_bytecode(lua_files: list, output_dir: Path):
    """Decompile bytecode, copy plaintext"""
    decompiled = []
    for lua_file in lua_files:
        with open(lua_file, 'rb') as f:
            header = f.read(4)
        if header == b'\x1bLua':
            out_file = (output_dir / lua_file.name).with_suffix('.lua')
            try:
                result = subprocess.run(
                    ["java", "-jar", str(UNLUAC_JAR), str(lua_file)],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0 and result.stdout.strip():
                    out_file.write_text(result.stdout)
                    decompiled.append(out_file)
                    print(f"[OK] {lua_file.name} -> {out_file.name}")
                else:
                    shutil.copy2(lua_file, output_dir)
                    decompiled.append(output_dir / lua_file.name)
            except Exception as e:
                print(f"[WARN] {lua_file.name}: {e}")
                shutil.copy2(lua_file, output_dir)
                decompiled.append(output_dir / lua_file.name)
        else:
            out_file = output_dir / lua_file.name
            if not out_file.suffix:
                out_file = out_file.with_suffix('.lua')
            shutil.copy2(lua_file, out_file)
            decompiled.append(out_file)
    return decompiled


def package_result(decompiled_files: list, resource_name: str, output_zip: Path):
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in decompiled_files:
            zf.write(f, f"{resource_name}/{f.name}")
    return output_zip


def process_resource(zip_path: str, license_key: str, callback=None):
    if callback:
        callback("Preparing server...")

    work_dir = Path(tempfile.mkdtemp(prefix="fivem_work_"))
    extract_dir = work_dir / "extract"
    output_dir = work_dir / "output"
    dump_dir = work_dir / "dump"
    extract_dir.mkdir()
    output_dir.mkdir()
    dump_dir.mkdir()

    try:
        resource_name = prepare_server(license_key, zip_path, str(extract_dir))
        if callback:
            callback(f"Resource: {resource_name}. Starting FXServer...")

        lua_files = start_fxserver_and_dump(license_key, resource_name, str(dump_dir), timeout=45)

        if not lua_files:
            raise RuntimeError("No Lua files were dumped. Check license key and resource.")

        if callback:
            callback(f"Dumped {len(lua_files)} files. Decompiling...")

        decompiled = decompile_bytecode(lua_files, output_dir)
        if callback:
            callback(f"Decompiled {len(decompiled)} files. Packaging...")

        output_zip = work_dir / f"{resource_name}_decompiled.zip"
        package_result(decompiled, resource_name, output_zip)

        if callback:
            callback("Done!")
        return str(output_zip)

    except Exception as e:
        if callback:
            callback(f"Error: {e}")
        raise
    finally:
        try:
            if 'resource_name' in locals():
                dest = CITIZEN_RESOURCES / resource_name
                if dest.exists():
                    shutil.rmtree(dest)
        except:
            pass
