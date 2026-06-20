"""
FiveM FXAP Decompiler — Core logic
Uses LD_PRELOAD hook (no GDB needed) to dump decrypted Lua from FXServer
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
from pathlib import Path

BASE_DIR = Path(__file__).parent
FXSERVER_DIR = Path("/opt/fxserver")
SERVER_DATA = Path("/opt/fxserver/server-data")
RESOURCES_DIR = SERVER_DATA / "resources"
UNLUAC_JAR = BASE_DIR / "unluac.jar"
HOOK_SO = BASE_DIR / "lua_hook.so"
RUN_SH = FXSERVER_DIR / "run.sh"


def prepare_server(license_key: str, resource_zip: str, extract_dir: str):
    """Set up FXServer with the resource and license key"""

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
        cfg = re.sub(r'set\s+sv_licenseKey\s+.*', f'set sv_licenseKey "{license_key}"', cfg)
        if 'sv_licenseKey' not in cfg:
            cfg = f'set sv_licenseKey "{license_key}"\n' + cfg
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


def start_fxserver_with_hook(dump_dir: str, timeout: int = 60):
    """Start FXServer with LD_PRELOAD hook, wait for dumps, then kill"""

    env = os.environ.copy()
    env["DUMP_DIR"] = dump_dir
    env["LD_PRELOAD"] = str(HOOK_SO)
    # Ensure the .so can find its dependencies
    env["TZ"] = "Asia/Shanghai"

    # Run via proot with LD_PRELOAD
    cmd = [
        "proot",
        "-S", str(FXSERVER_DIR / "alpine"),
        "--cwd=/opt/cfx-server",
        f"--bind={SERVER_DATA}:/opt/cfx-server/server-data",
        "/opt/cfx-server/FXServer",
        "+exec", "/opt/cfx-server/server-data/server.cfg"
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(FXSERVER_DIR)
    )

    # Wait for FXServer to load resources
    # Monitor dump_dir for new files
    start_time = time.time()
    last_count = 0
    stable_rounds = 0

    while time.time() - start_time < timeout:
        time.sleep(2)

        # Count dumped files
        lua_files = list(Path(dump_dir).glob("*.lua")) + list(Path(dump_dir).glob("*.bin"))
        current_count = len(lua_files)

        if current_count > last_count:
            last_count = current_count
            stable_rounds = 0
        elif current_count > 0:
            stable_rounds += 1
            # If no new files for 6 seconds, we're probably done
            if stable_rounds >= 3:
                break

        # Check if process died
        if proc.poll() is not None:
            break

    # Kill FXServer
    try:
        proc.send_signal(signal.SIGINT)
        time.sleep(1)
        if proc.poll() is None:
            proc.kill()
    except:
        pass

    # Collect stderr for debugging
    try:
        stderr = proc.stderr.read().decode(errors='replace')
        if stderr:
            print(f"[FXServer stderr] {stderr[-2000:]}")
    except:
        pass

    return list(Path(dump_dir).glob("*.lua")) + list(Path(dump_dir).glob("*.bin"))


def decompile_bytecode(lua_files: list, output_dir: Path):
    """Decompile .lua/.bin bytecode files with unluac, copy plaintext files as-is"""
    decompiled = []

    for lua_file in lua_files:
        with open(lua_file, 'rb') as f:
            header = f.read(4)

        if header == b'\x1bLua':
            # Bytecode — decompile
            out_file = output_dir / lua_file.name
            out_file = out_file.with_suffix('.lua')
            try:
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
            except Exception as e:
                print(f"[WARN] Failed to decompile {lua_file.name}: {e}")
                shutil.copy2(lua_file, output_dir)
                decompiled.append(output_dir / lua_file.name)
        else:
            # Plaintext — copy as-is
            out_file = output_dir / lua_file.name
            if not out_file.suffix:
                out_file = out_file.with_suffix('.lua')
            shutil.copy2(lua_file, out_file)
            decompiled.append(out_file)

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
    dump_dir = work_dir / "dump"
    extract_dir.mkdir()
    output_dir.mkdir()
    dump_dir.mkdir()

    try:
        # Step 1: Prepare server
        resource_name = prepare_server(license_key, zip_path, str(extract_dir))
        if callback:
            callback(f"Resource: {resource_name}. Starting FXServer with hook...")

        # Step 2: Start FXServer with LD_PRELOAD hook
        lua_files = start_fxserver_with_hook(str(dump_dir), timeout=90)

        if not lua_files:
            raise RuntimeError("No Lua files were dumped. Check license key and resource.")

        if callback:
            callback(f"Dumped {len(lua_files)} files. Decompiling...")

        # Step 3: Decompile
        decompiled = decompile_bytecode(lua_files, output_dir)
        if callback:
            callback(f"Decompiled {len(decompiled)} files. Packaging...")

        # Step 4: Package
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
        # Clean up resource from FXServer
        try:
            if 'resource_name' in locals():
                dest = RESOURCES_DIR / resource_name
                if dest.exists():
                    shutil.rmtree(dest)
        except:
            pass
