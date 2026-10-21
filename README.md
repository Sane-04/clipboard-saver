# 剪贴收藏 Clipboard Saver

一个 Windows 本地剪贴板工具：自动记录复制过的文字和图片，自带截图标注、滚动长截图，以及本地 OCR 文字提取和图片翻译。所有历史只保存在本机。

## 功能

- **剪贴板历史**：自动记录文字和静态图片，同一内容自动去重；可以按日期查询（今天、昨天、最近 7 天、自定义范围），也能按关键词搜索、固定、加特殊标记和标签；保留条数可以设置。
- **截图标注**：框选后还能移动或缩放选区；支持马赛克/实色打码、自由线/直线、箭头、文字，可撤销和重做。
- **滚动长截图**：默认由你手动滚动，程序自动拼接，采集时有实时预览；也可以开启自动滚动。
- **文字提取**：用 [RapidOCR](https://github.com/RapidAI/RapidOCR) 在本机识别，不上传图片；长图会分片识别。
- **图片翻译**：只在你主动点击时，调用你自己配置的 OpenAI 兼容接口。译文可以覆盖原文，绘制回图片上。
- **其他**：托盘常驻、可修改的全局快捷键、只运行一个实例、开机自启、把任意窗口置顶（图钉选窗）、独立图片查看器（Ctrl+滚轮缩放）。

## 默认快捷键

| 操作 | 快捷键 |
| --- | --- |
| 调出历史 | Ctrl+Shift+V |
| 截图 | Ctrl+Alt+X |
| 滚动长截图 | Ctrl+Alt+L |
| 完成编辑（复制并保存） | Ctrl+Enter |
| 撤销 / 重做 | Ctrl+Z / Ctrl+Y |

可以在「更多 → 快捷键设置」里修改。

## 从源码运行

需要 Windows 10/11 x64 和 Python 3.12。

```powershell
pip install -r requirements.txt
python setup_local_ocr.py   # 把 RapidOCR 自带的模型复制到 data/ocr-models/
python app.py
```

常用启动参数：

| 参数 | 说明 |
| --- | --- |
| `--hidden` | 启动后只显示托盘图标 |
| `--paused` | 以暂停记录模式启动 |
| `--data-dir <目录>` | 使用单独的数据目录（适合测试） |
| `--self-check <结果.json>` | 只用合成数据做运行自检 |

关闭历史窗口只会收起到托盘，程序继续记录；要彻底退出，请右键托盘图标选择「退出剪贴收藏」。

## 翻译接口

在「更多 → 文字识别与 API 设置」里填写 Base URL、API Key 和模型名。接口需要兼容 OpenAI 的 `/v1/chat/completions`。密钥用 Windows DPAPI 加密后保存在本机。只做文字提取不需要配置接口。

## 数据存放

源码运行时，数据默认放在项目下的 `data/` 目录（已被 `.gitignore` 排除），包括：

- `clipboard.sqlite3`：历史数据库
- `api-settings.json`：接口配置
- `ocr-models/`：OCR 模型
- `backups/`：备份

这几类位置都可以在「更多 → 数据保存位置」里分别改到别的目录。便携版的数据放在 EXE 旁边的 `data/` 里。

## 测试

```powershell
pip install pytest
python -m pytest -q
```

测试只使用临时数据库、合成图片和本机模拟接口，不会读取真实剪贴板、屏幕或调用外部 API。

## 打包便携版

```powershell
pip install -r packaging/requirements-build.txt
python packaging/build_portable.py --version 1.0.13 --output-dir <空目录>
```

打包结果是独立的 EXE、运行库和 OCR 模型，使用者不需要安装 Python。第三方许可证见 `packaging/licenses/`，便携版使用说明见 `packaging/使用说明.txt`。

## 项目结构

| 文件 | 作用 |
| --- | --- |
| `app.py` | 入口：托盘、全局快捷键、单实例 |
| `ui.py`、`ui_theme.py` | 历史主窗口与样式 |
| `storage.py` | SQLite 历史存储、日期归档、保留规则 |
| `clipboard_capture.py` | 剪贴板监听 |
| `screenshot.py`、`image_editor.py`、`image_document.py` | 截图与图片编辑器 |
| `long_capture.py`、`long_stitch.py`、`long_preview.py` | 滚动长截图 |
| `local_ocr.py`、`api_client.py`、`translation_session.py`、`text_results.py` | 文字提取与翻译 |
| `window_picker.py`、`window_pin.py`、`external_windows.py` | 窗口置顶 |
| `*_settings.py`、`data_locations.py` | 各类设置 |
| `startup_runtime.py`、`windows_integration.py` | DLL 初始化、Windows 集成 |
| `release_check.py` | 发布自检 |
| `packaging/` | PyInstaller 打包脚本 |
| `tests/` | 回归测试 |
