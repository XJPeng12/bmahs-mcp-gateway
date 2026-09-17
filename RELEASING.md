# 发布指南（PyPI）

`bmahs-mcp-gateway` 发布到 PyPI 的完整流程。发新版照着走一遍即可，全程约 5 分钟。

## 0. 前置条件

- [pypi.org](https://pypi.org) 账号（TestPyPI 与正式站账号不通用）。
- API token：[pypi.org/manage/account](https://pypi.org/manage/account/) → API tokens → Add token → 范围选整个账号或指定项目。token 形如 `pypi-…`。
- **token 安全**：只在发布命令里临时使用，不写入任何文件、不提交 git、不发到聊天里；用完（或误泄露）到同一页面撤销即可，撤销不影响已发布的版本。

## 1. 发版步骤

### 1.1 改版本号（单源）

版本号只改一处：`src/bmahs_mcp/__init__.py` 的 `__version__`，`pyproject.toml` 通过 hatch 动态取它。版本语义建议：修 bug → 三位 +1；新增向后兼容能力 → 第二位 +1。

### 1.2 提交并打 tag

```bash
git add -A && git commit -m "release: v0.x.y"
git tag v0.x.y && git push && git push --tags
```

### 1.3 构建

```bash
D:\uv\uv.exe build --out-dir dist
```

> ⚠️ 坑：本仓库是外层 uv workspace 的成员，在包目录里直接 `uv build` 产物会落到**外层仓库根**的 `dist/`，必须显式加 `--out-dir dist`。

### 1.4 发布前检查产物

```bash
# wheel 应只含 bmahs_mcp/* + dist-info；sdist 多 tests/README/LICENSE
D:/uv/uv.exe run --no-project python -c "import zipfile,glob; [print(n) for n in zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist()]"
tar tzf dist/*.tar.gz
```

### 1.5 发布

```bash
D:\uv\uv.exe publish --token <你的pypi-开头的token>
```

默认上传正式 PyPI（`https://upload.pypi.org/legacy/`）。首次发新版本**不需要**任何预注册，PyPI 会自动认领项目（首次发布者即 owner）。

### 1.6 验证

```bash
# 元数据上线（几秒内生效）
curl -s https://pypi.org/pypi/bmahs-mcp-gateway/json | head -c 200

# 干净环境安装 + 冒烟（验证最小依赖可用）
D:/uv/uv.exe venv .tmp-pypi-check
D:/uv/uv.exe pip install --python .tmp-pypi-check/Scripts/python.exe --index-url https://pypi.org/simple/ bmahs-mcp-gateway
.tmp-pypi-check/Scripts/bmahs-mcp.exe --version
.tmp-pypi-check/Scripts/bmahs-mcp.exe discover --seconds 2
rm -rf .tmp-pypi-check   # Windows Git Bash；PowerShell 用 Remove-Item -Recurse
```

页面检查：https://pypi.org/project/bmahs-mcp-gateway/ ——描述渲染、GitHub 仓库链接（来自 `pyproject.toml` 的 `[project.urls]`）、左侧 "Navigation" 的 Download files。

## 2. 本次发布踩过的坑（备忘）

| 坑 | 处理 |
| --- | --- |
| `mcp>=1.2.0` 下限与 2.x API 不符 | 依赖下限必须是 `mcp>=2.2.0`（代码用构造参数 handler，1.x 没有） |
| `pillow` 混进核心依赖 | 核心包不 import pillow（那是 mobile/ 的事），勿加回 |
| uvicorn 隐式依赖 | stdio 模式不需要；HTTP 模式做成 `[http]` extra，缺失时报中文提示并退出码 2 |
| GitHub 上 token 明文出现过 | 发布完成后撤销重建（见第 0 节） |

## 3. 发新版速查（复制即用）

```bash
# ① 改 src/bmahs_mcp/__init__.py 的 __version__
# ② 提交 + tag + push
git add -A && git commit -m "release: vX.Y.Z" && git tag vX.Y.Z && git push && git push --tags
# ③ 构建 + 发布
D:/uv/uv.exe build --out-dir dist
D:/uv/uv.exe publish --token <新token>
# ④ 验证
curl -s -o /dev/null -w "%{http_code}\n" https://pypi.org/pypi/bmahs-mcp-gateway/json
```
