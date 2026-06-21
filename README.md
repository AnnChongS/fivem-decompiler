# FiveM FXAP Server-Side Decryptor

自动解密 FiveM 服务端加密脚本 (FXAP)。上传加密 ZIP + CFX License Key → 下载明文 Lua。

## ⚠️ 限制

**只能解密 server-side 脚本** (`server_scripts` + `shared_scripts`)。`client_scripts` 需要 FiveM 客户端（Windows + GTA V）才能解密，本工具无法处理。

## 快速部署

```bash
git clone https://github.com/AnnChongS/fivem-decompiler.git
cd fivem-decompiler
sudo bash start.sh
```

打开 `http://你的IP:8080`，上传 ZIP + 填 CFX Key 即可。

## 系统要求

- **架构**: x86_64 (FXServer 不支持 ARM)
- **内存**: 4GB+ (FXServer 运行需要)
- **OS**: Debian/Ubuntu (其他需手动装依赖)
- **网络**: 需要访问 cfx.re 验证 license key

## 原理

```
上传 ZIP + CFX Key
    ↓
解析 fxmanifest → 提取 server_scripts + shared_scripts
    ↓
去掉 dependencies/外部引用 → 复制到 FXServer system_resources
    ↓
启动 FXServer (带 ensure) + GDB attach (动态查找 getS)
    ↓
getS hook → dump 解密后的 Lua 字节码
    ↓
Java 21 + unluac 反编译字节码 → 明文 Lua
    ↓
标识符匹配重组 → 打包 ZIP 下载
```

### 关键技术

- **getS hook**: FXServer 在解析加密 Lua 时调用 `getS` 读取解密内容，GDB 在此处断点 dump
- **动态地址查找**: GDB Python 脚本轮询 `/proc/PID/maps` 自动定位 `libcitizen-scripting-lua.so` 中的 getS
- **manifest 清理**: 自动去掉 `dependencies`、`@oxmysql`、`@ox_lib` 等引用，让 FXServer 能加载资源
- **内容匹配**: 用文件路径标识符 + 反编译内容中的变量名做匹配，不依赖硬编码关键字

## 服务管理

```bash
systemctl status fxdecrypt    # 查看状态
systemctl restart fxdecrypt   # 重启
journalctl -u fxdecrypt -f    # 查看日志
```

## 文件结构

```
/opt/fxdecrypt/
  app.py              # Web 服务主程序
  venv/               # Python 虚拟环境 (Flask)
  uploads/            # 上传的 ZIP
  results/            # 解密结果 ZIP
/opt/fxserver/         # FXServer 安装
/tmp/unluac.jar        # Lua 5.4 反编译器 (已 patch)
```
