# FiveM FXAP Decompiler

Web service that decompiles FXAP-encrypted FiveM resources into readable Lua source code.

## Quick Start

```bash
git clone <this-repo>
cd fivem-decompiler
sudo bash deploy.sh
```

Open `http://YOUR_IP:8080` in browser, upload `.zip` + enter license key.

## How It Works

1. Upload encrypted resource zip + FiveM license key
2. FXServer starts the resource (decrypts FXAP at runtime)
3. GDB hooks `luaL_loadbufferx` to capture decrypted bytecode
4. Patched `unluac` decompiles FiveM Lua 5.4 bytecode to source
5. Download decompiled zip

## Requirements

- x86_64 Linux (Ubuntu/Debian)
- 4GB+ RAM (FXServer needs ~1GB)
- Ports: 8080 (web), 30120 (FXServer, internal)

## Components

| File | Description |
|------|-------------|
| `app.py` | FastAPI web server |
| `decompiler.py` | Core decompile pipeline |
| `gdb_dump.py` | GDB script for runtime Lua dump |
| `unluac.jar` | Patched Lua 5.4 decompiler (FiveM types) |
| `deploy.sh` | One-click deployment |

## Key Technical Detail

FiveM's Lua 5.4 fork inserts `LUA_TVECTOR=4`, shifting all type codes:

| Type | Standard Lua 5.4 | FiveM Lua 5.4 |
|------|-----------------|---------------|
| SHORT_STRING | 4 | **5** |
| LONG_STRING | 20 | **21** |
| VECTOR2 | N/A | **4** |
| VECTOR3 | N/A | **20** |
| BLOB_STRING | N/A | **37** |

The patched `unluac` handles these shifted type codes.

## License

For educational purposes. Only decrypt resources you own or have permission to decrypt.
