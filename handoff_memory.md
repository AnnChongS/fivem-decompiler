# FiveM FXAP Server-Side Decryptor — Handoff

## 当前状态：Web 解密服务已部署 ✅

### 🌐 Web 解密服务
- **地址**: http://64.90.0.146:8080
- **Systemd**: `fxdecrypt.service` (开机自启)
- **代码**: `/opt/fxdecrypt/app.py` (593 行)
- **GitHub**: https://github.com/AnnChongS/fivem-decompiler
- **功能**: 上传加密 ZIP + CFX Key → 自动解密 server/shared 脚本 → 下载

### 能力范围
- ✅ `server_scripts` — 完全解密 (FXAP → Lua 字节码 → 明文)
- ✅ `shared_scripts` — 完全解密
- ✅ 明文文件 — 直接保留
- ❌ `client_scripts` — 服务端无法解密，需要 FiveM 客户端

## 服务器信息

- **IP**: `64.90.0.146`
- **密码**: `oAEjFtFz5BIXk1aT`
- **OS**: Debian 12 x86_64, 3.8GB RAM
- **FXServer**: Build 25770
- **Java**: Adoptium JRE 21 (`/opt/jdk-21+35-jre/bin/java`)
- **unluac**: `/tmp/unluac.jar`

## Web 服务架构

```
用户上传 ZIP + CFX Key
    ↓
解析 fxmanifest (server_scripts + shared_scripts)
    ↓
strip_manifest: 去掉 dependencies, @oxmysql, @ox_lib 等
    ↓
复制资源到 citizen/system_resources/
    ↓
启动 FXServer (server.cfg 含 ensure) + GDB attach (动态 getS)
    ↓
getS hook → dump 解密字节码
    ↓
Java 21 + unluac 反编译
    ↓
重组 (框架过滤 → 标识符匹配 → 贪心分配)
    ↓
打包 ZIP 下载
```

## 关键技术点

1. **getS 动态查找**: GDB Python 脚本轮询 `/proc/PID/maps`，找到 `libcitizen-scripting-lua.so` 后计算 getS 地址 (偏移 0x188550)
2. **FXAP 两种加密**: server/shared getS 完全解密；client 只去 18 字节头，内容仍加密
3. **manifest 清理**: 自动去掉 `dependencies {}`、`dependency`、`@oxmysql/...`、`@ox_lib/...` 行
4. **框架过滤**: 跳过 >50KB dump (sessionmanager) 和 <50B dump (placeholder)
5. **匹配策略**: 文件路径标识符 + 反编译内容顶层变量名 + 文件名核心词，不依赖硬编码关键字
6. **Java 21 必需**: unluac.jar 编译版本 65.0，Java 17 不支持

## 踩坑记录

1. **资源依赖导致无法加载**: FXServer 找不到 oxmysql/ox_lib 就拒绝启动资源 → strip_manifest 去掉依赖
2. **框架 dump 干扰匹配**: sessionmanager 文件 >50KB 混在 dump 里 → 按大小过滤
3. **硬编码标记只对特定资源有效**: `storageunit`/`nui` 等标记只适用于 sf_storageunits → 改用通用标识符匹配
4. **小 placeholder dump 误导**: 15B/90B 的 `-- placeholder` dump 干扰 → 最小 50B 过滤
5. **加密大小 ≠ 反编译大小**: 1.8KB 加密 → 4.7KB 反编译，完全不成比例 → 不能用大小匹配
6. **FIFO 必须保持写端打开**: `sleep infinity > fifo` 是关键
7. **ASLR 必须关闭**: `echo 0 > /proc/sys/kernel/randomize_va_space`

## 解密测试记录

### sf_storageunits (第二次)
- server/main.lua: ✅ 2571 行
- shared/shared.lua: ✅ 246 行
- client/main.lua: ❌ 服务端无法解密

### 6630f7f4 (accent system)
- server/sv_main.lua: ✅ 106 行
- server/sv_esxfix.lua: ✅ 90 行
- server/sv_utils.lua: ✅ 7 行
- shared/framework.lua: ✅ 254 行
- shared/init.lua: ✅ 105 行
- shared/locale.lua: ✅ 75 行
- shared/safety.lua: ✅ 424 行
- client/*.lua: ❌ 服务端无法解密
