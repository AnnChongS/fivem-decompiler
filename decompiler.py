"""
FiveM FXAP Decompiler — Core logic
Handles: extract → FXServer start → GDB dump → unluac → package
"""

import os
import sys
import time
import shutil
import signal
import zipfile
import hashlib
import subprocess
import tempfile
import re
from pathlib import Path

BASE_DIR = Path(__file__).parent
FXSERVER_DIR = Path("/opt/fxserver")
SERVER_DATA = Path("/opt/fxserver/server-data")
RESOURCES_DIR = SERVER_DATA / "resources"
DUMP_SCRIPT = BASE_DIR / "gdb_dump.py"
UNLUAC_JAR = BASE_DIR / "unluac.jar"

# Will be auto-detected on first run
LUALOADBUFFERX_OFFSET = None


def find_lualoadbufferx_offset():
    """Auto-detect luaL_loadbufferx offset in libcitizen-scripting-lua54.so"""
    global LUALOADBUFFERX_OFFSET

    so_path = None
    for p in FXSERVER_DIR.rglob("citizen/scripting/lua/liblua*.so"):
        so_path = p
        break
    if not so_path:
        for p in FXSERVER_DIR.rglob("libcitizen-scripting-lua54.so"):
            so_path = p
            break
    if not so_path:
        raise RuntimeError("Cannot find libcitizen-scripting-lua54.so in FXServer")

    # Search for getS reader function pattern:
    #   48 8b 47 08    mov rax, [rdi+8]
    #   48 8b 0f       mov rcx, [rdi]
    #   48 29 47 08    sub [rdi+8], rax
    #   4c 89 47 00    mov [rdi], r8
    #   48 85 c0       test rax, rax
    #   c3             ret
    data = so_path.read_bytes()

    # Pattern: mov rax,[rdi+8]; mov rcx,[rdi]; sub [rdi+8],rax; mov [rdi],r8
    pattern = bytes([0x48, 0x8b, 0x47, 0x08, 0x48, 0x8b, 0x0f, 0x48, 0x29, 0x47, 0x08])

    offsets = []
    start = 0
    while True:
        idx = data.find(pattern, start)
        if idx == -1:
            break
        offsets.append(idx)
        start = idx + 1

    if not offsets:
        raise RuntimeError("Cannot find getS pattern in .so file")

    # luaL_loadbufferx is typically ~0x30-0x50 bytes after getS
    # Search for 48 8d 35 (lea rsi, [rip+...]) near getS
    for gets_off in offsets:
        search_start = gets_off
        search_end = min(gets_off + 0x200, len(data))
        chunk = data[search_start:search_end]

        # Look for lea rsi, [rip+...] which is how luaL_loadbufferx typically starts
        for i in range(0, len(chunk) - 3, 1):
            if chunk[i:i+3] == bytes([0x48, 0x8d, 0x35]):
                offset = search_start + i
                LUALOADBUFFERX_OFFSET = offset
                print(f"[+] Found luaL_loadbufferx at offset 0x{offset:x}")
                return offset

    raise RuntimeError("Cannot find luaL_loadbufferx near getS")


def prepare_server(license_key: str, resource_zip: str, extract_dir: str):
    """Set up FXServer with the resource and license key"""

    # Extract resource
    resource_name = None
    with zipfile.ZipFile(resource_zip, 'r') as zf:
        zf.extractall(extract_dir)

    # Find fxmanifest.lua or __resource.lua
    for root, dirs, files in os.walk(extract_dir):
        if 'fxmanifest.lua' in files or '__resource.lua' in files:
            resource_name = os.path.basename(root)
            resource_path = root
            break

    if not resource_name:
        raise RuntimeError("No fxmanifest.lua or __resource.lua found in zip")

    # Copy resource to FXServer resources
    dest = RESOURCES_DIR / resource_name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(resource_path, dest)

    # Update server.cfg with license key
    cfg_path = SERVER_DATA / "server.cfg"
    if cfg_path.exists():
        cfg = cfg_path.read_text()
        # Replace or add license key
        cfg = re.sub(r'set\s+sv_licenseKey\s+.*', f'set sv_licenseKey "{license_key}"', cfg)
        if 'sv_licenseKey' not in cfg:
            cfg = f'set sv_licenseKey "{license_key}"\n' + cfg
        # Ensure resource is started
        if f'ensure {resource_name}' not in cfg:
            cfg += f'\nensure {resource_name}\n'
        cfg_path.write_text(cfg)
    else:
        cfg_path.write_text(f'''# Auto-generated server config
sv_hostname "FXAP Decompiler"
sv_licenseKey "{license_key}"
sv_maxclients 1
endpoint_add_tcp "0.0.0.0:30120"
endpoint_add_udp "0.0.0.0:30120"

startmap fivem-map-hipster

ensure mapmanager
ensure chat
ensure spawnmanager
ensure sessionmanager
ensure hardcap
ensure {resource_name}
''')

    return resource_name


def start_fxserver_with_gdb():
    """Start FXServer under GDB and dump all luaL_loadbufferx calls"""

    offset = LUALOADBUFFERX_OFFSET or find_lualoadbufferx_offset()

    # Update the dump script with detected offset
    dump_script_content = DUMP_SCRIPT.read_text()
    dump_script_content = re.sub(
        r'RXP_OFFSET\s*=\s*0x[0-9a-fA-F]+',
        f'RXP_OFFSET = 0x{offset:x}',
        dump_script_content
    )
    DUMP_SCRIPT.write_text(dump_script_content)

    dump_dir = Path(tempfile.mkdtemp(prefix="fivem_dump_"))

    # Start FXServer under GDB
    run_sh = FXSERVER_DIR / "run.sh"
    if not run_sh.exists():
        # Find the actual run script
        for candidate in ["run.sh", "fxserver", "FXServer"]:
            p = FXSERVER_DIR / candidate
            if p.exists():
                run_sh = p
                break

    env = os.environ.copy()
    env["DUMP_DIR"] = str(dump_dir)

    # Use GDB batch mode with the dump script
    gdb_cmd = [
        "gdb", "-batch",
        "-x", str(DUMP_SCRIPT),
        "--args", str(run_sh),
        "+exec", str(SERVER_DATA / "server.cfg"),
        "+set", "citizen_dir", str(FXSERVER_DIR / "citizen")
    ]

    proc = subprocess.Popen(
        gdb_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(SERVER_DATA)
    )

    return proc, dump_dir


def wait_for_dumps(proc, dump_dir: Path, timeout: int = 120):
    """Wait for GDB to finish dumping, then kill FXServer"""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Timeout is expected — FXServer runs forever, we kill it after dumps are done
        pass
    finally:
        try:
            proc.send_signal(signal.SIGINT)
            time.sleep(2)
            proc.kill()
        except:
            pass

    # Collect dumped files
    lua_files = list(dump_dir.rglob("*.lua"))
    print(f"[+] Dumped {len(lua_files)} Lua files")
    return lua_files


def decompile_bytecode(lua_files: list, output_dir: Path):
    """Decompile .lua bytecode files with unluac, copy plaintext files as-is"""
    decompiled = []

    for lua_file in lua_files:
        # Check if it's bytecode (starts with \x1bLua) or plaintext
        with open(lua_file, 'rb') as f:
            header = f.read(4)

        if header == b'\x1bLua':
            # Bytecode — decompile
            out_file = output_dir / lua_file.name
            result = subprocess.run(
                ["java", "-jar", str(UNLUAC_JAR), str(lua_file)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0 and result.stdout.strip():
                out_file.write_text(result.stdout)
                decompiled.append(out_file)
            else:
                # Decompile failed — copy original
                shutil.copy2(lua_file, output_dir)
                decompiled.append(output_dir / lua_file.name)
        else:
            # Plaintext — copy as-is
            shutil.copy2(lua_file, output_dir)
            decompiled.append(output_dir / lua_file.name)

    return decompiled


def package_result(decompiled_files: list, resource_name: str, output_zip: Path):
    """Package decompiled files into a zip"""
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in decompiled_files:
            zf.write(f, f"{resource_name}/{f.name}")

    return output_zip


def process_resource(zip_path: str, license_key: str, callback=None):
    """Main pipeline: zip + key → decompiled zip"""

    if callback:
        callback("Preparing server...")

    work_dir = Path(tempfile.mkdtemp(prefix="fivem_work_"))
    extract_dir = work_dir / "extract"
    output_dir = work_dir / "output"
    extract_dir.mkdir()
    output_dir.mkdir()

    try:
        # Step 1: Prepare server
        resource_name = prepare_server(license_key, zip_path, str(extract_dir))
        if callback:
            callback(f"Resource: {resource_name}. Starting FXServer...")

        # Step 2: Start FXServer with GDB dump
        proc, dump_dir = start_fxserver_with_gdb()
        if callback:
            callback("FXServer running, dumping encrypted Lua...")

        # Step 3: Wait for dumps
        lua_files = wait_for_dumps(proc, dump_dir)
        if not lua_files:
            raise RuntimeError("No Lua files were dumped. Check license key and resource.")

        if callback:
            callback(f"Dumped {len(lua_files)} files. Decompiling...")

        # Step 4: Decompile
        decompiled = decompile_bytecode(lua_files, output_dir)
        if callback:
            callback(f"Decompiled {len(decompiled)} files. Packaging...")

        # Step 5: Package
        output_zip = work_dir / f"{resource_name}_decompiled.zip"
        package_result(decompiled, resource_name, output_zip)

        if callback:
            callback("Done!")

        return str(output_zip)

    except Exception as e:
        if callback:
            callback(f"Error: {e}")
        raise
