#!/bin/bash
# 共享启动预检（U-05）：被 start_ticket_grabbing.sh / run_from_prompt.sh source。
#
# 职责：
#   preflight_check_adb          — adb 存在性检查；ANDROID_HOME 软化（有 adb 即放行）
#   preflight_check_placeholders — 模板占位符黑名单（报错前移到任何配置改写之前）
#   preflight_check_device       — serial 精确预检（adb -s <serial> get-state）
#
# 逃生口：
#   HATICKETS_SKIP_PREFLIGHT=1        全部预检跳过
#   HATICKETS_SKIP_SERIAL_PREFLIGHT=1 只跳过 serial 精确校验（无线 adb / 特殊 transport）
#
# 注意：本文件的 grep/sed 解析 JSONC 属于宽松加速层，Python 层
# mobile/config.py 的 validate_config_dict 才是权威判定；两层结论
# 不一致时以 Python 为准（Python 层不会漏判）。
# 兼容 set -u：所有环境变量引用一律使用 ${VAR:-} 形式。

preflight_check_adb() {
    [ "${HATICKETS_SKIP_PREFLIGHT:-0}" = "1" ] && return 0
    if ! command -v adb >/dev/null 2>&1; then
        # PATH 里没有 adb 才尝试常见 SDK 目录兜底（brew 用户在此之前已放行）
        for _sdk in "${ANDROID_HOME:-}" "$HOME/Library/Android/sdk" "$HOME/Android/Sdk" "/opt/android-sdk"; do
            if [ -n "$_sdk" ] && [ -x "$_sdk/platform-tools/adb" ]; then
                export ANDROID_HOME="$_sdk"
                export ANDROID_SDK_ROOT="$_sdk"
                export PATH="$_sdk/platform-tools:$PATH"
                break
            fi
        done
    fi
    if ! command -v adb >/dev/null 2>&1 && [ -n "${CONDA_PREFIX:-}" ]; then
        # adbutils 的 PyPI wheel 自带 adb；本项目的 Conda 环境可能只有
        # 这一份二进制。自动纳入 PATH，避免 launchd/非交互 shell 丢失 adb。
        for _adb in "$CONDA_PREFIX"/lib/python*/site-packages/adbutils/binaries/adb; do
            if [ -x "$_adb" ]; then
                export PATH="$(dirname "$_adb"):$PATH"
                break
            fi
        done
    fi
    if ! command -v adb >/dev/null 2>&1; then
        echo "❌ 未找到 adb 命令。任选其一："
        echo "   1) brew install android-platform-tools"
        echo "   2) 安装 Android Studio 后 export ANDROID_HOME=\"\$HOME/Library/Android/sdk\""
        return 1
    fi
    # adb 已在 PATH 即放行；ANDROID_HOME 缺失不再是错误（U-05 软化）
    if [ -n "${ANDROID_HOME:-}" ]; then
        export ANDROID_SDK_ROOT="$ANDROID_HOME"
    fi
    return 0
}

# $1=key $2=config_file → 打印字符串值（null / 缺失 / 非字符串输出空）
# 注意：不能用 `t;s/.*//` 的分支写法——macOS BSD sed 把分号后内容当 label，
# 报 undefined label 且输出为空，导致 serial 预检在 macOS 静默失效（CI 实测）。
# `-n` + `p` 仅在替换成功时打印，GNU/BSD 行为一致。
_preflight_extract_string_field() {
    grep -E "^[[:space:]]*\"$1\"[[:space:]]*:" "$2" 2>/dev/null | head -1 \
        | sed -E -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"([^\"]*)\".*/\1/p"
}

# $1=config_file
preflight_check_placeholders() {
    [ "${HATICKETS_SKIP_PREFLIGHT:-0}" = "1" ] && return 0
    local bad=""
    grep -q "你的设备序列号" "$1" 2>/dev/null && bad="$bad serial"
    grep -q "你的真实观演人姓名" "$1" 2>/dev/null && bad="$bad users"
    if [ -n "$bad" ]; then
        # 注意 ${bad} 必须带大括号：macOS bash 3.2 会把紧随其后的全角字符
        # 首字节并入变量名，导致变量值和半个多字节字符一起消失（真机实测）。
        echo "❌ 配置文件仍是模板占位符（字段:${bad}）: $1"
        echo "   serial → 填 adb devices 输出的序列号；users → 填大麦 App 中已保存的观演人真实姓名"
        echo "   （本次未修改配置、未连接设备）"
        return 1
    fi
    return 0
}

# $1=config_file
preflight_check_device() {
    [ "${HATICKETS_SKIP_PREFLIGHT:-0}" = "1" ] && return 0
    local serial state
    serial="$(_preflight_extract_string_field serial "$1")"
    if [ -n "$serial" ] && [ "${HATICKETS_SKIP_SERIAL_PREFLIGHT:-0}" != "1" ]; then
        state="$(adb -s "$serial" get-state 2>/dev/null)"
        if [ "$state" != "device" ]; then
            echo "❌ 配置的 serial=$serial 不在线（get-state: ${state:-连接失败}）"
            echo "   当前在线设备清单："
            adb devices 2>/dev/null | sed 1d | sed '/^$/d' | sed 's/^/     /'
            echo "   无线 adb / 特殊连接可设 HATICKETS_SKIP_SERIAL_PREFLIGHT=1 跳过此校验"
            return 1
        fi
        echo "✅ 目标设备在线: $serial"
    else
        # serial 为 null/缺失（单设备自动识别）或显式跳过精确校验 → 泛化检查
        if ! adb devices 2>/dev/null | grep -q "device$"; then
            echo "❌ 未检测到已连接的 Android 设备"
            echo "   请通过 USB 连接设备并开启 USB 调试模式"
            return 1
        fi
        echo "✅ Android 设备连接正常"
    fi
    return 0
}
