# NapCat-WordLibBot 插件仓库

基于 NapCat 的 QQ 机器人框架（[NapCat-WordLibBot](https://github.com/Bdlxx/NapCat-WordLibBot)）的独立插件仓库。

主仓库安装脚本（install.sh）在部署/更新时会自动从本仓库拉取插件到 `plugins/` 目录，
无需手动下载。**词库插件（wordlib.py）作为核心插件保留在主仓库**，其余插件都在本仓库。

## 插件列表

| 文件 | 插件名 | 说明 |
|---|---|---|
| `marry.py` | 结婚插件 | 群内每日结婚/离婚系统 |
| `jm_downloader.py` | JM下载 | `jm` 命令下载禁漫本子并转 PDF 分享，自动更新 jmcomic 库（配合 `jm_worker.py` 子进程使用） |
| `jm_worker.py` | JM下载工作进程 | 由 jm_downloader 以子进程方式调用，非独立插件（无 handle 函数，不会被主程序加载） |
| `pseudo_persona.py` | 伪人插件 | AI 对话回复、角色扮演，支持 GLM 和 Gemini 双模型 |
| `video_parser.py` | 视频解析 | 抖音/B站/快手/小红书/TikTok 视频解析去水印 |

## 插件规范（SDK）

每个插件是一个独立的 `.py` 文件，放入 `plugins/` 目录后主程序自动加载：

1. 导出 `handle(event: dict) -> bool` 函数（返回 `True` 表示已处理该事件）
2. 声明 SDK 元数据变量：

```python
__plugin_name_cn__ = "插件中文名"
__plugin_name_en__ = "plugin_name"
__plugin_version__ = "1.0.0"
__plugin_desc__ = "插件功能描述"
__plugin_author__ = "作者"
```

3. 配置存于 `data/<name_en>_config.json`（`commands` / `settings` / `messages` 三段式），
   Web 面板自动识别展示
4. 插件开关：各插件自定义开关命令（通常为「开启/关闭<插件名>」，仅主人可用，群内=本群开关、私聊=全局），
   也可在 Web 面板群组开关表格中控制（新插件自动出现，无需手动注册）

## 手动安装/更新

```bash
git clone --depth 1 https://github.com/Bdlxx/NapCat-WordLibBot-Plugins.git /tmp/napbot_plugin_repo
cp /tmp/napbot_plugin_repo/*.py /root/mybot_<QQ>/plugins/
```

或直接运行主仓库的 `install.sh`（部署/更新实例时自动拉取）。
