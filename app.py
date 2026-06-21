#!/usr/bin/env python3
"""FXAP Server-Side Lua Auto-Decryption Web Service v3

Key fix: GDB script dynamically finds getS address and sets breakpoint
from within GDB, so it's ready before resource loading completes.
"""

import os, re, time, shutil, subprocess, zipfile, uuid, threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

BASE = Path('/opt/fxdecrypt')
UPLOADS, RESULTS = BASE / 'uploads', BASE / 'results'
FXSERVER = Path('/opt/fxserver')
UNLUAC, JAVA21 = Path('/tmp/unluac.jar'), Path('/opt/jdk-21.0.11+10-jre/bin/java')
SYSTEM_RES = FXSERVER / 'alpine/opt/cfx-server/citizen/system_resources'
SERVER_CFG = FXSERVER / 'alpine/opt/cfx-server/server-data/server.cfg'
GETS_OFFSET = 0x188550

decrypt_lock = threading.Lock()
jobs = {}


def upd(jid, **kw):
    if jid in jobs: jobs[jid].update(kw)


def kill_fx():
    subprocess.run(['pkill', '-9', '-f', 'FXServer'], capture_output=True)
    subprocess.run(['pkill', '-9', '-f', 'gdb'], capture_output=True)
    subprocess.run(['pkill', '-9', '-f', 'sleep infinity'], capture_output=True)
    time.sleep(2)



def strip_manifest(mf_path):
    """Strip dependencies and external script refs from fxmanifest so FXServer can load the resource."""
    txt = mf_path.read_text(errors='replace')
    lines = txt.split(chr(10))
    out = []
    skip_block = False
    brace_depth = 0
    for ln in lines:
        t = ln.strip()
        # Skip single dependency lines
        if t.startswith('dependency '):
            continue
        # Skip dependencies block
        if re.match(r'dependencies\s*\{', t):
            skip_block = True
            brace_depth = t.count('{') - t.count('}')
            if brace_depth <= 0:
                skip_block = False
            continue
        if skip_block:
            brace_depth += t.count('{') - t.count('}')
            if brace_depth <= 0:
                skip_block = False
            continue
        # Strip @oxmysql/... and @ox_lib/... lines
        if re.match(r"\s*'@(oxmysql|ox_lib|ox)/.*',?\s*$", t):
            continue
        if re.match(r'\s*"@(oxmysql|ox_lib|ox)/.*",?\s*$', t):
            continue
        out.append(ln)
    mf_path.write_text(chr(10).join(out), encoding='utf-8')

def run_pipeline(jid, zpath, key):
    wd, rd = UPLOADS / jid / 'work', UPLOADS / jid / 'result'
    wd.mkdir(exist_ok=True); rd.mkdir(exist_ok=True)
    fx = gdb = None; dest = None

    try:
        # 0: Extract
        upd(jid, step=0, progress=5, message='解压...')
        ed = wd / 'res'
        with zipfile.ZipFile(zpath) as zf: zf.extractall(str(ed))
        mf = next(ed.rglob('fxmanifest.lua'), None)
        if not mf: raise Exception('无 fxmanifest.lua')
        root = mf.parent; rname = root.name

        # 0.5: Strip manifest (remove deps so FXServer can load)
        strip_manifest(mf)

        # 1: Parse
        upd(jid, step=1, progress=10, message='解析 fxmanifest...')
        sscripts = parse_scripts(mf)
        if not sscripts: raise Exception('无 server_scripts')

        # 2: Prepare
        upd(jid, step=2, progress=15, message='准备资源...')
        dest = SYSTEM_RES / rname
        if dest.exists(): shutil.rmtree(str(dest))
        shutil.copytree(str(root), str(dest))

        # server.cfg WITH ensure — resource loads at startup
        SERVER_CFG.write_text(
            f'sv_maxclients 1\nendpoint_add_tcp 0.0.0.0:30120\n'
            f'endpoint_add_udp 0.0.0.0:30120\nset sv_licenseKey {key}\nensure {rname}\n')

        # 3: Start FXServer + GDB (parallel)
        upd(jid, step=3, progress=20, message='启动 FXServer + GDB...')
        kill_fx()
        subprocess.run('echo 0 > /proc/sys/kernel/randomize_va_space', shell=True)
        subprocess.run(['rm', '-f', '/tmp/fxstdin_w'])
        os.mkfifo('/tmp/fxstdin_w')
        kp = subprocess.Popen('sleep infinity > /tmp/fxstdin_w', shell=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ddir = f'/tmp/fdump_{jid}'
        shutil.rmtree(ddir, ignore_errors=True)

        # GDB hook script that DYNAMICALLY finds getS address
        gscript = create_gdb_hook_script(ddir)
        gpath = f'/tmp/gdb_{jid}.py'
        with open(gpath, 'w') as f: f.write(gscript)

        # Wrapper script: start FXServer, poll for library, attach GDB ASAP
        wrapper = f'''#!/bin/bash
cd /opt/fxserver
bash run.sh +exec alpine/opt/cfx-server/server-data/server.cfg \
    < /tmp/fxstdin_w > /tmp/fxsrv_{jid}.log 2>&1 &
FX_PID=$!

# Poll for library to load (fast, 0.5s intervals)
for i in $(seq 1 60); do
    if grep -q 'r-xp.*libcitizen-scripting-lua' /proc/$FX_PID/maps 2>/dev/null; then
        break
    fi
    sleep 0.5
done

# Attach GDB immediately
gdb -batch \
    -ex 'set pagination off' \
    -ex 'set confirm off' \
    -ex "attach $FX_PID" \
    -ex 'source {gpath}' \
    -ex 'continue' \
    > /tmp/gdb_{jid}.log 2>&1 &

wait $FX_PID 2>/dev/null
'''
        wrapper_path = f'/tmp/wrapper_{jid}.sh'
        with open(wrapper_path, 'w') as f: f.write(wrapper)
        os.chmod(wrapper_path, 0o755)

        logf = open('/tmp/fxsrv_wrapper.log', 'w')
        fx = subprocess.Popen(['bash', wrapper_path], stdout=logf, stderr=logf)

        # Wait for GDB READY
        upd(jid, step=4, progress=35, message='等待 GDB 就绪...')
        gdb_log = f'/tmp/gdb_{jid}.log'
        for i in range(30):
            time.sleep(1)
            try:
                with open(gdb_log) as f:
                    content = f.read()
                    if 'READY' in content:
                        upd(jid, message='GDB 就绪，等待 dump...')
                        break
            except: pass

        # 4: Wait for dumps
        upd(jid, step=5, progress=55, message=f'等待 {rname} dump...')
        for i in range(60):
            time.sleep(1)
            dfs = list(Path(ddir).glob('*.bin')) if Path(ddir).exists() else []
            if len(dfs) >= 3:
                time.sleep(5)  # Let remaining dumps finish
                break
            upd(jid, message=f'等待 dump... ({i+1}s, {len(dfs)} files)')

        # Cleanup
        if fx: fx.kill()
        kp.kill()
        kill_fx()

        dfs = sorted(Path(ddir).glob('*.bin')) if Path(ddir).exists() else []
        if not dfs: raise Exception('无 dump 文件')
        upd(jid, message=f'Dump: {len(dfs)} 文件')

        # 5: Decompile
        upd(jid, step=6, progress=75, message='反编译...')
        dec = decompile(ddir)
        ok = sum(1 for v in dec.values() if not v.startswith('-- '))
        upd(jid, message=f'反编译: {ok}/{len(dec)}')

        # 6: Reassemble
        upd(jid, step=7, progress=90, message='重组...')
        out = rd / rname
        reassemble(root, sscripts, dec, out, ddir)
        zout = RESULTS / f'{jid}.zip'
        zipdir(out, zout)
        upd(jid, status='done', progress=100, message='完成！', result_path=str(zout))

    except Exception as e:
        upd(jid, status='error', message=str(e))
    finally:
        for p in [fx, kp]:
            if p:
                try: p.kill()
                except: pass
        kill_fx()
        if dest and dest.exists():
            try: shutil.rmtree(str(dest))
            except: pass
        decrypt_lock.release()


def create_gdb_hook_script(ddir):
    """Create GDB Python hook script that dynamically finds getS and hooks it."""
    return f'''import gdb, os, time

DUMP_DIR = "{ddir}"
os.makedirs(DUMP_DIR, exist_ok=True)

# Dynamically find getS address by polling /proc/PID/maps
pid = gdb.selected_inferior().pid
hook_addr = None
for i in range(60):
    try:
        with open('/proc/%d/maps' % pid) as f:
            for line in f:
                if 'libcitizen-scripting-lua.so' in line and 'r-xp' in line:
                    parts = line.split()
                    base = int(parts[0].split('-')[0], 16)
                    offset = int(parts[2], 16)
                    hook_addr = base + ({GETS_OFFSET} - offset)
                    gdb.write('[LIB] hook=0x%x\\n' % hook_addr)
                    break
        if hook_addr:
            break
    except:
        pass
    time.sleep(0.5)

if not hook_addr:
    gdb.write('[ERROR] Library not found!\\n')
else:
    n = [0]
    class H(gdb.Breakpoint):
        def stop(self):
            n[0] += 1
            rsi = int(gdb.parse_and_eval('(long)$rsi'))
            try:
                ptr = int(gdb.parse_and_eval('*(long*)' + str(rsi)))
                sz = int(gdb.parse_and_eval('*(long*)' + str(rsi + 8)))
            except:
                ptr, sz = 0, 0
            gdb.write('[#%d] buf=0x%x size=%d\\n' % (n[0], ptr, sz))
            if ptr and sz > 10:
                try:
                    d = gdb.selected_inferior().read_memory(ptr, min(sz, 500000)).tobytes()
                    p = '%s/%04d.bin' % (DUMP_DIR, n[0])
                    open(p, 'wb').write(d)
                    gdb.write('[SAVED] %d -> %s\\n' % (len(d), p))
                except:
                    pass
            return False

    H('*0x%x' % hook_addr)
    gdb.write('[READY]\\n')
'''


def parse_scripts(mf):
    txt = mf.read_text(errors='replace')
    results = []
    for bn in ['server_scripts', 'server_script', 'shared_scripts', 'shared_script']:
        s = []; ins = False; d = 0
        for ln in txt.split('\n'):
            t = ln.strip()
            if re.match(rf'{bn}\s*\{{', t):
                ins = True; d = 1; a = t.split('{', 1)[1]
                if '}' in a:
                    s.extend(re.findall(r"'([^']+)'|\"([^\"]+)\"", a))
                    ins = False
                continue
            if ins:
                d += t.count('{') - t.count('}')
                if d <= 0: ins = False; continue
                s.extend(re.findall(r"'([^']+)'|\"([^\"]+)\"", t))
        results.extend([x[0] or x[1] for x in s])
    return results


def decompile(dd):
    r = {}; dp = Path(dd)
    if not dp.exists(): return r
    for bf in sorted(dp.glob('*.bin')):
        data = bf.read_bytes()
        if data[:4] == b'\x1bLua':
            try:
                p = subprocess.run([str(JAVA21), '-jar', str(UNLUAC), str(bf)],
                                   capture_output=True, text=True, timeout=30)
                r[bf.name] = p.stdout if p.returncode == 0 and p.stdout.strip() else f'-- ERR: {p.stderr[:200]}'
            except Exception as e:
                r[bf.name] = f'-- EXC: {e}'
        else:
            try:
                t = data.decode('utf-8')
                r[bf.name] = t if any(k in t for k in ['function', 'local ', 'return', 'if ']) \
                    else f'-- NOTLUA ({data[:4].hex()})'
            except:
                r[bf.name] = f'-- BIN ({data[:4].hex()}, {len(data)}B)'
    return r


def reassemble(root, sscripts, dec, out, ddir):
    out.mkdir(parents=True, exist_ok=True)
    all_lua = [f for f in root.rglob('*.lua') if f.name != 'fxmanifest.lua']
    dp = Path(ddir)

    # Filter framework dumps (>50KB are sessionmanager/fxrunner framework)
    FRAMEWORK_MAX = 50000
    filtered_dec = {}
    for dn, c in dec.items():
        bf = dp / dn
        if bf.exists() and bf.stat().st_size > FRAMEWORK_MAX:
            continue  # Skip framework dumps
        filtered_dec[dn] = c

    # Separate dumps into text and bytecode categories
    sorted_dumps = sorted(filtered_dec.keys())
    text_dumps, byte_dumps = {}, {}
    for dn in sorted_dumps:
        c = filtered_dec[dn]
        if c.startswith('-- '):  # decompilation error or binary marker
            continue
        bf = dp / dn
        if bf.exists() and bf.read_bytes()[:4] == b'\x1bLua':
            byte_dumps[dn] = c
        else:
            text_dumps[dn] = c

    # Identify encrypted files that need decryption
    needs_dec = set()
    for f in all_lua:
        rel = str(f.relative_to(root)).replace(chr(92), '/')
        for pat in sscripts:
            rx = pat.replace(chr(92), '/').replace('.', r'\.').replace('**', '.*').replace('*', '[^/]*').replace('?', '.')
            if re.match(rx + '$', rel):
                needs_dec.add(rel)
                break

    text_files, encrypted_files = [], []
    for f in all_lua:
        rel = str(f.relative_to(root)).replace(chr(92), '/')
        if rel not in needs_dec:
            continue
        try:
            hdr = f.read_bytes()[:4]
            if hdr == b'FXAP' or hdr[:2] == b'\x09\x5a':
                encrypted_files.append(rel)
            else:
                text_files.append(rel)
        except:
            encrypted_files.append(rel)

    file_map = {}
    used = set()

    # Match text files (non-encrypted) to text dumps by identifier overlap
    for rel in text_files:
        try:
            otxt = (Path(root) / rel).read_text(errors='replace')
            oids = set(re.findall(r'\b([a-zA-Z_]\w{3,})\b', otxt))
        except:
            continue
        if len(oids) < 3:
            continue
        best, best_score = None, 0
        for dn, c in text_dumps.items():
            if dn in used:
                continue
            dids = set(re.findall(r'\b([a-zA-Z_]\w{3,})\b', c))
            if not dids:
                continue
            score = len(oids & dids) / len(oids | dids)
            if score > best_score:
                best_score = score
                best = dn
        if best and best_score > 0.15:
            file_map[rel] = filtered_dec[best]
            used.add(best)

    # Match encrypted files to bytecode dumps
    remaining_enc = [r for r in encrypted_files if r not in file_map]
    remaining_byte = [dn for dn in byte_dumps.keys() if dn not in used]

    # Strategy 1: 1-to-1 shortcut
    if len(remaining_enc) == 1 and len(remaining_byte) >= 1:
        # Pick the best matching bytecode dump by identifier overlap with file path
        rel = remaining_enc[0]
        rel_ids = set(re.findall(r'\b([a-zA-Z_]\w{2,})\b', rel.lower()))
        best_dn, best_score = None, 0
        for dn in remaining_byte:
            dids = set(re.findall(r'\b([a-zA-Z_]\w{3,})\b', filtered_dec[dn].lower()))
            common = len(rel_ids & dids)
            if common > best_score:
                best_score = common
                best_dn = dn
        if best_dn:
            file_map[rel] = filtered_dec[best_dn]
            remaining_enc = []
            remaining_byte = [dn for dn in remaining_byte if dn != best_dn]

# Strategy 2: Enhanced matching - content identifiers + file name context
    if remaining_enc and remaining_byte:
        dump_ids = {}
        dump_names = {}
        for dn in remaining_byte:
            content = filtered_dec[dn]
            ids = set(re.findall(r'\b([a-zA-Z_]\w{3,})\b', content))
            dump_ids[dn] = set(x.lower() for x in ids)
            top_names = set()
            for line in content.split(chr(10))[:15]:
                for m in re.findall(r'^([A-Z][a-zA-Z_]+)\s*=', line):
                    top_names.add(m.lower())
                for m in re.findall(r'^local\s+function\s+(\w+)', line):
                    top_names.add(m.lower())
            dump_names[dn] = top_names

        scored_pairs = []
        for rel in remaining_enc:
            path_parts = set(re.split(r'[/\._]', rel.lower()))
            path_ids = path_parts - {'lua', 'server', 'client', 'shared'}
            file_stem = Path(rel).stem.lower()
            file_core = re.sub(r'^(sv|cl|sh|client|server|shared)_?', '', file_stem)

            for dn in remaining_byte:
                dids = dump_ids.get(dn, set())
                dnames = dump_names.get(dn, set())
                if not dids:
                    continue
                score = 0
                common = len(path_ids & dids)
                score += common * 2
                if file_core and len(file_core) > 3:
                    for name in dnames:
                        if file_core in name or name in file_core:
                            score += 5
                    for did in dids:
                        if file_core in did:
                            score += 1
                if 'locales' in dids and 'locales' not in path_ids:
                    score -= 3
                if score > 0:
                    scored_pairs.append((score, rel, dn))

        scored_pairs.sort(reverse=True)
        used_enc = set()
        used_byte = set()
        for score, rel, dn in scored_pairs:
            if rel in used_enc or dn in used_byte:
                continue
            file_map[rel] = filtered_dec[dn]
            used_enc.add(rel)
            used_byte.add(dn)
        remaining_enc = [r for r in remaining_enc if r not in used_enc]
        remaining_byte = [dn for dn in remaining_byte if dn not in used_byte]
    # Strategy 3: Size-based fallback for any remaining
    for rel in remaining_enc:
        if not remaining_byte:
            break
        if len(remaining_enc) == 1 and len(remaining_byte) == 1:
            file_map[rel] = filtered_dec[remaining_byte[0]]
            remaining_byte.clear()
            continue
        best_dn = remaining_byte[0]
        if best_dn:
            file_map[rel] = filtered_dec[best_dn]
            remaining_byte.remove(best_dn)

    # Copy all files: use decompiled content for matched files, original for others
    for f in root.rglob('*'):
        if f.is_dir():
            continue
        rel = f.relative_to(root)
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        rs = str(rel).replace(chr(92), '/')
        if f.name == 'fxmanifest.lua':
            shutil.copy2(str(f), str(dest))
        elif rs in file_map:
            dest.write_text(file_map[rs], encoding='utf-8')
        else:
            shutil.copy2(str(f), str(dest))


def zipdir(src, dst):
    with zipfile.ZipFile(str(dst), 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in src.rglob('*'):
            if f.is_file(): zf.write(str(f), str(f.relative_to(src)))


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/decrypt', methods=['POST'])
def start_decrypt():
    if not decrypt_lock.acquire(blocking=False):
        return jsonify({'error': '已有任务在运行'}), 409
    file = request.files.get('file'); cfx_key = request.form.get('cfx_key', '').strip()
    if not file or not file.filename.endswith('.zip'):
        decrypt_lock.release(); return jsonify({'error': '请上传 .zip'}), 400
    if not cfx_key.startswith('cfxk_'):
        decrypt_lock.release(); return jsonify({'error': 'Key 格式错'}), 400
    jid = str(uuid.uuid4())[:8]; d = UPLOADS / jid; d.mkdir(parents=True, exist_ok=True)
    z = d / 'resource.zip'; file.save(str(z))
    jobs[jid] = {'status': 'running', 'step': 0, 'progress': 5, 'message': '处理中...', 'result_path': None}
    threading.Thread(target=run_pipeline, args=(jid, str(z), cfx_key), daemon=True).start()
    return jsonify({'job_id': jid})

@app.route('/api/status/<jid>')
def status(jid):
    j = jobs.get(jid)
    return jsonify(j) if j else ('Not found', 404)

@app.route('/api/download/<jid>')
def dl(jid):
    j = jobs.get(jid)
    if not j or j['status'] != 'done': return ('Not ready', 404)
    return send_file(j['result_path'], as_attachment=True, download_name=f'decrypted_{jid}.zip')


HTML_TEMPLATE = '''<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FXAP Decryptor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);min-height:100vh;color:#e0e0e0;display:flex;align-items:center;justify-content:center}
.c{background:rgba(255,255,255,.05);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.1);border-radius:20px;padding:40px;width:520px;max-width:95vw;box-shadow:0 20px 60px rgba(0,0,0,.5)}
h1{text-align:center;margin-bottom:8px;font-size:1.6em}
.sub{text-align:center;color:#888;margin-bottom:30px;font-size:.9em}
.fg{margin-bottom:20px}
label{display:block;margin-bottom:6px;font-weight:600;color:#aaa;font-size:.85em;text-transform:uppercase;letter-spacing:1px}
input[type=file],input[type=text]{width:100%;padding:12px 16px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);border-radius:10px;color:#fff;font-size:14px;outline:none}
input:focus{border-color:#6c63ff}
input[type=file]::file-selector-button{background:#6c63ff;color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;margin-right:12px}
.btn{width:100%;padding:14px;background:linear-gradient(135deg,#6c63ff,#3b82f6);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;margin-top:10px}
.btn:hover{transform:translateY(-2px);box-shadow:0 8px 25px rgba(108,99,255,.4)}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}
.pa{display:none;margin-top:24px;background:rgba(0,0,0,.3);border-radius:12px;padding:20px;border:1px solid rgba(255,255,255,.08)}
.pbb{width:100%;height:6px;background:rgba(255,255,255,.1);border-radius:3px;overflow:hidden;margin:12px 0}
.pb{height:100%;background:linear-gradient(90deg,#6c63ff,#3b82f6);border-radius:3px;transition:width .5s;width:0}
.st{font-size:14px;color:#aaa;min-height:20px}
.sl{margin-top:16px;font-size:13px}
.s{padding:4px 0;color:#666}.s.a{color:#6c63ff;font-weight:600}.s.d{color:#4ade80}.s.e{color:#ef4444}
.dl{display:none;margin-top:20px;width:100%;padding:14px;background:linear-gradient(135deg,#4ade80,#22c55e);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;text-decoration:none;text-align:center}
</style></head><body>
<div class="c">
<h1>&#x1f513; FXAP Decryptor</h1><p class="sub">自动解密 FiveM 服务端加密脚本</p>
<form id="f" enctype="multipart/form-data">
<div class="fg"><label>&#x1f4e6; 加密资源 ZIP</label><input type="file" name="file" accept=".zip" required></div>
<div class="fg"><label>&#x1f511; CFX License Key</label><input type="text" name="cfx_key" placeholder="cfxk_xxx" required></div>
<button type="submit" class="btn" id="b">&#x1f680; 开始解密</button>
</form>
<div class="pa" id="pa"><div class="st" id="st">准备...</div><div class="pbb"><div class="pb" id="pb"></div></div><div class="sl" id="sl"></div></div>
<a class="dl" id="dl" href="#">&#x1f4e5; 下载解密资源</a>
</div>
<script>
const S=['解析 fxmanifest','准备资源','启动 FXServer','GDB Hook','Dump','反编译','打包'];
document.getElementById('f').addEventListener('submit',async e=>{
e.preventDefault();const fd=new FormData(e.target),b=document.getElementById('b'),pa=document.getElementById('pa'),dl=document.getElementById('dl');
b.disabled=true;b.textContent='\u23f3 上传中...';pa.style.display='block';dl.style.display='none';rs(-1);
try{const r=await fetch('/api/decrypt',{method:'POST',body:fd}),d=await r.json();
if(!r.ok){er(d.error);return}poll(d.job_id)}catch(e){er('网络错误: '+e.message)}});
function rs(i,ei=-1){document.getElementById('sl').innerHTML=S.map((s,j)=>{let c='s';
if(j<i)c+=' d';else if(j===i)c+=' a';if(j===ei)c+=' e';
const p=j<i?'\u2705':j===i?'\u23f3':'\u2b1c';
return '<div class="'+c+'">'+p+' '+s+'</div>'}).join('')}
async function poll(id){const st=document.getElementById('st'),pb=document.getElementById('pb'),b=document.getElementById('b'),dl=document.getElementById('dl');
while(true){await new Promise(r=>setTimeout(r,2000));
try{const r=await fetch('/api/status/'+id),d=await r.json();
st.textContent=d.message||d.status;pb.style.width=(d.progress||0)+'%';
if(d.step!==undefined)rs(d.step);
if(d.status==='done'){rs(S.length);st.textContent='\u2705 完成！';b.disabled=false;b.textContent='\u1f680 开始解密';
dl.style.display='block';dl.href='/api/download/'+id;return}
if(d.status==='error'){rs(d.step||0,d.step||0);st.textContent='\u274c '+d.message;b.disabled=false;b.textContent='\u1f680 开始解密';return}
}catch(e){st.textContent='连接中断...'}}}
function er(m){document.getElementById('st').textContent='\u274c '+m;document.getElementById('b').disabled=false;document.getElementById('b').textContent='\u1f680 开始解密'}
</script></body></html>'''

if __name__ == '__main__':
    UPLOADS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    app.run(host='0.0.0.0', port=8080, debug=False)
