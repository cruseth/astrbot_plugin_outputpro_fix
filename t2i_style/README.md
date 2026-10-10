# notebook-maple

用于 `pillowmd.LoadMarkdownStyles()` 的仿真笔记本样式。它把 Markdown 渲染成一页干净的浅米色横线笔记纸，带红色边距线、左侧装订孔、细纸张阴影和偏工作笔记的排版节奏。

## 风格特点

- 适合普通聊天记录、说明文档、教程、清单、代码解释和长文本回复。
- 背景是低饱和浅纸色，不使用默认深蓝底，整体更清爽。
- 正文、标题、表格和代码块都优先保证清晰度，避免花纹干扰阅读。
- 代码块使用深色底，和正文纸张形成明显层级。
- 横线和装订孔只作为轻装饰，目标是像真实课堂/会议笔记而不是花哨主题。

## 字体

当前目录实际提供两个字体文件：

- `fonts/OPPOSans-Regular.ttf`：`setting.yml` 默认使用的中文、英文、数字和代码字体。
- `fonts/DejaVuSansMono.ttf`：默认备用等宽字体。

不要在配置中填写未随本插件交付的字体文件；如自行替换字体，请同步修改 `setting.yml` 的字体字段。

## 使用方式

在 AstrBot WebUI 的本插件配置里，把 `t2i.pillowmd_style_dir` 填成：

```text
data/plugins/astrbot_plugin_outputpro_fix/t2i_style
```

注意填目录，不要填 `setting.yml` 文件本身。

当前 `elements.yml` 的背景功能为关闭状态；如需启用背景图片，请自行准备配置中引用的资源。

## 预览

预览图会生成在本目录的 `preview-notebook.png`，emoji 测试图会生成在 `preview-emoji.png`。
