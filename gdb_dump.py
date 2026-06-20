"""
FiveM FXAP Lua Dumper — GDB Python script
Attaches to FXServer and dumps all decrypted Lua buffers from luaL_loadbufferx

Environment variables:
  DUMP_DIR — where to save dumped files (default: /tmp/fivem_dump/)
  RXP_OFFSET — override getS offset (auto-detected if not set)
"""

import gdb
import os
import sys
import re

DUMP_DIR = os.environ.get("DUMP_DIR", "/tmp/fivem_dump/")
SO_NAME = "libcitizen-scripting-lua54.so"
RXP_OFFSET = int(os.environ.get("RXP_OFFSET", "0"), 16) if os.environ.get("RXP_OFFSET") else None

os.makedirs(DUMP_DIR, exist_ok=True)

dump_count = 0
bp_set = False


class LuaLoadBP(gdb.Breakpoint):
    """Breakpoint at luaL_loadbufferx entry — dumps the decrypted buffer."""

    def __init__(self, spec):
        super().__init__(spec, internal=True)
        self.silent = True

    def stop(self):
        global dump_count
        try:
            # System V AMD64 ABI: rdi=L, rsi=buf, rdx=size, rcx=name, r8=mode
            buf_ptr = int(gdb.parse_and_eval("$rsi"))
            buf_size = int(gdb.parse_and_eval("$rdx"))
            name_ptr = int(gdb.parse_and_eval("$rcx"))

            inferior = gdb.selected_inferior()

            # Read chunk name
            try:
                name_bytes = bytes(inferior.read_memory(name_ptr, 2048))
                name = name_bytes.split(b'\x00')[0].decode('utf-8', errors='replace')
            except:
                name = f"unknown_{dump_count}"

            # Read buffer content
            try:
                buf = bytes(inferior.read_memory(buf_ptr, buf_size))
            except Exception as e:
                sys.stderr.write(f"[ERR] read buf: {e}\n")
                sys.stderr.flush()
                return False

            if len(buf) < 5:
                return False

            # Skip if still FXAP encrypted
            if buf[:4] == b'FXAP':
                return False

            # Skip non-Lua content (NUI files, etc.)
            if buf[:4] != b'\x1bLua' and not buf[:10].strip().startswith((b'local ', b'function', b'--', b'return', b'if ')):
                return False

            # Create filename from chunk name
            safe = name
            for prefix in ['@', './', '=']:
                if safe.startswith(prefix):
                    safe = safe[len(prefix):]
            safe = re.sub(r'[^a-zA-Z0-9._\-/]', '_', safe)
            safe = safe.replace('/', '__').strip('_')
            if not safe:
                safe = f"unnamed_{dump_count}"

            # Ensure .lua extension
            if not safe.endswith('.lua'):
                safe += '.lua'

            filepath = os.path.join(DUMP_DIR, safe)
            c = 1
            base_p = filepath
            while os.path.exists(filepath):
                n, e = os.path.splitext(base_p)
                filepath = f"{n}_{c}{e}"
                c += 1

            with open(filepath, "wb") as f:
                f.write(buf)

            dump_count += 1
            is_bytecode = buf[:4] == b'\x1bLua'
            tag = "BYTECODE" if is_bytecode else "SOURCE"
            sys.stderr.write(f"\n[DUMP #{dump_count}] {tag} {filepath} ({len(buf)} bytes)\n")
            sys.stderr.write(f"  chunk='{name}'\n")
            sys.stderr.flush()

        except Exception as e:
            sys.stderr.write(f"[DUMP ERR] {e}\n")
            sys.stderr.flush()

        return False  # Don't stop execution


def find_and_set_bp():
    """Find library in /proc/PID/maps and set breakpoint."""
    global bp_set, RXP_OFFSET

    pid = gdb.selected_inferior().pid
    maps_path = f"/proc/{pid}/maps"

    try:
        with open(maps_path) as f:
            for line in f:
                if SO_NAME in line and 'r-xp' in line:
                    parts = line.split()
                    map_start = int(parts[0].split('-')[0], 16)

                    if RXP_OFFSET:
                        # Use provided offset
                        bp_addr = map_start + RXP_OFFSET
                        LuaLoadBP(f"*{hex(bp_addr)}")
                        bp_set = True
                        sys.stderr.write(f"\n[OK] {SO_NAME} at r-xp {hex(map_start)}\n")
                        sys.stderr.write(f"[OK] luaL_loadbufferx at {hex(bp_addr)} (manual offset)\n")
                        sys.stderr.flush()
                        return True
                    else:
                        # Auto-detect: search for getS reader pattern
                        # Pattern: mov rax,[rdi+8]; mov rcx,[rdi]; sub [rdi+8],rax
                        # 48 8b 47 08 48 8b 0f 48 29 47 08
                        inferior = gdb.selected_inferior()
                        pattern = bytes([0x48, 0x8b, 0x47, 0x08, 0x48, 0x8b, 0x0f, 0x48, 0x29, 0x47, 0x08])

                        # Read .so file to find pattern
                        so_path = None
                        for p in [f"/proc/{pid}/map_files/"]:
                            pass  # We'll search in memory instead

                        # Search in the mapped region
                        search_size = 0x200000  # 2MB should be enough
                        try:
                            mem = bytes(inferior.read_memory(map_start, search_size))
                            idx = mem.find(pattern)
                            if idx >= 0:
                                gets_addr = map_start + idx
                                # Search forward for lea rsi, [rip+...] (48 8d 35)
                                search_chunk = mem[idx:idx+0x200]
                                for j in range(0, len(search_chunk) - 3, 1):
                                    if search_chunk[j:j+3] == bytes([0x48, 0x8d, 0x35]):
                                        bp_addr = gets_addr + j
                                        LuaLoadBP(f"*{hex(bp_addr)}")
                                        bp_set = True
                                        sys.stderr.write(f"\n[OK] {SO_NAME} at r-xp {hex(map_start)}\n")
                                        sys.stderr.write(f"[OK] luaL_loadbufferx at {hex(bp_addr)} (auto-detected)\n")
                                        sys.stderr.flush()
                                        return True
                        except Exception as e:
                            sys.stderr.write(f"[WARN] memory search failed: {e}\n")
                            sys.stderr.flush()
    except FileNotFoundError:
        pass

    return False


# ---- Main ----
gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set disable-randomization off")
gdb.execute("set breakpoint pending on")

sys.stderr.write("[GDB] Starting FXServer dump...\n")
sys.stderr.flush()

gdb.execute("set stop-on-solib-events 1")

first_run = True
max_iters = 500
iteration = 0

while iteration < max_iters:
    iteration += 1

    try:
        if first_run:
            gdb.execute("run")
            first_run = False
        else:
            gdb.execute("continue")
    except gdb.error as e:
        sys.stderr.write(f"[GDB] exec error: {e}\n")
        sys.stderr.flush()
        break

    if not bp_set:
        if find_and_set_bp():
            gdb.execute("set stop-on-solib-events 0")
            sys.stderr.write("[GDB] Running — dumping all luaL_loadbufferx calls\n")
            sys.stderr.flush()
            gdb.execute("continue")
            break
    else:
        gdb.execute("continue")

sys.stderr.write(f"\n[GDB] Done. Total dumps: {dump_count}\n")
sys.stderr.flush()
