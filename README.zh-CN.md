# Omarchy Clipper

[English](README.md) | 简体中文

Omarchy Clipper 可从 Omarchy 状态栏保存文字与图片混合的收藏。浏览器扩展支持捕获可阅读的文章、仓库 README、论坛页面、选中内容和截图。文章捕获后可在本地离线阅读。

> 当前为源码发布候选版本，尚未上架 Omarchy 插件商店。

[安装](#添加构建与安装) · [使用](#使用) · [设置](#设置) · [兼容性](#兼容性与验证) · [更新](#更新) · [卸载](#卸载)

## 界面截图

实际程序界面，使用虚构的示例内容，不包含个人收藏数据。

### 收藏列表

![Clipper 收藏列表与编辑区](assets/screenshots/collection.png)

### 设置

![预览与语言设置](assets/screenshots/settings.png)

### 悬浮预览

![悬浮预览内容，截图仅包含预览窗口](assets/screenshots/preview.png)

## 功能

- 将文字与图片保存在同一条收藏中；打开已有收藏不会替换尚未完成的草稿。
- 捕获网页可阅读内容以供本地离线阅读，个人笔记与捕获的文章正文分开保存。
- 在收藏列表侧边预览内容，无封面收藏也可预览。
- 桌面界面支持中文与英文，浏览器扩展共用语言偏好。
- 删除的收藏先进入回收站，明确清空前可恢复。

## 依赖

- 支持 Quattro 插件命令的 Omarchy，以及 Quickshell 运行时
- C++20 编译器、GNU Make、`pkg-config` 和 `wl-clipboard`
- Qt 6 Core、Gui、Widgets、Sql、Qml、Quick、QuickControls2 和 WebEngineQuick
- 用于嵌入界面翻译的 Qt Linguist（`lrelease6`）与 Qt 资源编译器（`rcc`）
- libxml2 开发文件
- 用于构建浏览器扩展的 Node.js 和 npm
- 用于网页捕获的 Chromium 和／或 Brave Origin Nightly

配套程序的安装与卸载需要 Python 3。请先通过系统包管理器安装依赖，再进行构建；插件不会自行安装系统软件包或请求提权。

GNU `source-highlight` 为可选依赖，用于支持的代码块语法着色；未安装时仍可阅读代码。区域捕获使用 Omarchy 已有的截图命令及其已安装的截图工具。

## 兼容性与验证

2026-10-18 验证的开发环境为 Omarchy `4.0.0.r7026.g51bfd1d-1`（`omarchy-dev`）、Quickshell `0.3.2-6`、Qt Core/Declarative `6.12.0`、Qt WebEngine `6.11.2`、Node.js `26.7.0` 和 npm `11.19.0`。这些是已测试版本，不代表最低版本要求。较早的 Omarchy 版本可能缺少本插件使用的共享原生设置控件。

实机验证使用顶部状态栏、1920 × 1080 逻辑像素与 2× 缩放。跨物理显示器移动及其他状态栏位置尚未完成实机验证。自动定位测试不代表已兼容所有桌面配置。

Clipper 以当前用户身份运行。Omarchy 插件在长期运行的 shell 中执行，没有沙箱隔离；配套进程读写配置的本地收藏目录。浏览器扩展仅在请求捕获时读取页面，并将捕获结果发送给已注册的本地原生宿主。启用前请检查源码与安装计划。

## 商店提交草稿

仅在验证准备提交的确切发布候选版本后使用以下元数据。

摘要：从 Omarchy 状态栏保存混合收藏、捕获可阅读网页，以供本地离线使用。

分类：Productivity

标签：Bar, Quickshell

## 迁移旧版复制安装

如果早期开发版本将插件直接复制到 Omarchy 插件目录，请在禁用前保留其设置。在包含 `scripts/uninstall.py` 且与旧安装匹配的源码检出目录中运行：

```sh
MIGRATION_SOURCE="$(mktemp -d)" &&
git clone --depth 1 https://github.com/Lcryolite/omarchy-clipper "$MIGRATION_SOURCE" &&
python3 "$MIGRATION_SOURCE/scripts/install.py" --migrate-settings &&
omarchy plugin disable lcryolite.clipper &&
python3 scripts/uninstall.py --dry-run &&
python3 scripts/uninstall.py
```

如果克隆或设置迁移失败，命令链会在禁用旧控件前停止。迁移时不要传入 `--purge-data-dir`。普通卸载会保留持久设置和收藏。之后继续下方的 Git 添加、构建和配套程序安装步骤；安装器可识别已知旧版布局，不会覆盖无关插件。

如果卸载器提示旧版配套文件与当前检出不匹配，请使用匹配的旧版检出，或构建当前组件后，带上已配置的扩展 ID 运行一次安装器。该迁移会记录准确的文件归属，让后续卸载能移除组件，而无需猜测同名文件的归属。

## 添加、构建与安装

先检查本仓库，再添加插件，暂不启用。配套组件准备完成前，请拒绝交互式启用提示。

```sh
omarchy plugin add https://github.com/Lcryolite/omarchy-clipper
```

在 Git 管理的检出目录中构建：

```sh
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/lcryolite.clipper"
cd "$PLUGIN_DIR"
make -j2 all
npm ci --ignore-scripts
make extension
npm run typecheck
omarchy plugin validate .
```

在使用的每个浏览器中，以“加载已解压的扩展程序”方式加载 `extension/`，记录各自的 32 字符扩展 ID。保持检出目录位置不变，以维持 ID 稳定。先预览配套组件安装计划，再使用已配置浏览器的 ID 执行安装：

```sh
python3 scripts/install.py \
  --chromium-extension-id CHROMIUM_EXTENSION_ID \
  --brave-extension-id BRAVE_EXTENSION_ID \
  --dry-run
python3 scripts/install.py \
  --chromium-extension-id CHROMIUM_EXTENSION_ID \
  --brave-extension-id BRAVE_EXTENSION_ID
omarchy plugin enable lcryolite.clipper
```

未配置某个浏览器时，省略对应选项。旧版 `--extension-id` 选项会为两个受支持浏览器渠道注册同一个 ID。安装器只修改用户拥有的目标位置，无需 `sudo`。默认将 `clipper` 和配套的混合收藏详情程序 `clipper-native` 安装到 `${HOME}/.local/bin`，并记录两者以便安全更新和卸载。修改目标位置前，安装器会验证 `wl-clipboard`、两个已构建程序、全部扩展输出，以及插件、扩展、应用和原生辅助程序的版本是否匹配。

注册完成后，重新加载各个已解压的扩展。

## 使用

点击 Omarchy 状态栏中的回形针打开 Clipper。在编辑区输入文字或粘贴图片，点击**保存**，或按 **Ctrl+Enter**。新收藏会出现在列表中。点击已有收藏可打开阅读器或详情窗口。通过搜索框查找收藏内容；在回收站恢复已删除收藏或明确清空。按 **Escape** 关闭面板。

也可通过 shell 打开和关闭同一面板：

```sh
omarchy-shell shell summon lcryolite.clipper '{}'
omarchy-shell shell hide lcryolite.clipper
```

捕获网页时，在来源页面打开扩展弹窗，选择可用的捕获操作。浏览器捕获与桌面编辑区保存文字是不同的操作。

## 设置

点击面板标题栏的单色齿轮进入设置。修改立即生效，并保存到 Clipper 的持久配置中；返回箭头可回到收藏列表。设置文件位于 `${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-clipper/settings.json`，独立于 Omarchy 状态栏布局和收藏目录。普通禁用或移除插件会保留该文件。如果保存失败，Clipper 会显示错误，并保留上次有效的已保存设置。

| 设置 | 默认值 | 行为 |
| --- | --- | --- |
| 悬浮预览 | 关闭 | 开启后，将鼠标悬停在收藏条目上即可查看内容，无封面也可预览。鼠标可移入预览框滚动；离开条目及预览框后关闭。 |
| 预览首选侧边 | 右侧 | 可选左侧或右侧。空间不足时自动换边或缩小；两侧均放不下时隐藏，可点击收藏阅读。关闭预览会禁用这些控件，但保留偏好。 |
| 界面语言 | 跟随系统 | 可选跟随系统、中文或 English。系统语言不受支持时使用英文。扩展下次打开弹窗时应用共享语言；不会翻译已保存内容。 |

高级配置支持 `dataDir`（默认为 `""`，使用常规收藏目录）和 `executable`（默认为 `"clipper"`）。先关闭面板，再使用插件检出目录中的辅助工具设置其中一项或两项。自定义收藏目录或可执行文件时请使用绝对路径：

```sh
SETTINGS_HELPER="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/lcryolite.clipper/clipper-settings.py" &&
python3 "$SETTINGS_HELPER" set dataDir '"/absolute/path/to/collection"' &&
python3 "$SETTINGS_HELPER" set executable '"/absolute/path/to/clipper"'
```

恢复两项默认值：

```sh
SETTINGS_HELPER="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/lcryolite.clipper/clipper-settings.py" &&
python3 "$SETTINGS_HELPER" set dataDir '""' &&
python3 "$SETTINGS_HELPER" set executable '"clipper"'
```

辅助工具的直接修改会在下次打开面板时载入。每个值都是 JSON 字符串；命令失败时，先前保存的设置保持不变。

## 数据与网络行为

默认收藏目录为 `${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-clipper`，包含收藏、文章笔记、归属于收藏的图片、回收站和未完成草稿。维护前请停止正在执行的 Clipper 请求，并备份整个目录。

捕获网页可能通过浏览器获取页面内容和图片。阅读已捕获文章在本地离线完成。Clipper 不提供云同步服务。

悬浮预览标题和说明按纯文本显示，不会加载标题中嵌入的远程图片；预览只显示已经保存的本地图片。

自动捕获会拒绝本地主机名、非公网 IP 字面地址（包括 IPv4 映射 IPv6）、带账号信息的 URL，以及图片重定向。图片请求需要对应来源的访问权限，且不携带凭据或来源页信息。这些 URL 检查不会解析或固定 DNS 地址；浏览器的私网访问策略是额外边界，不能保证请求严格只到达公网地址。

每张图片下载有覆盖响应头和响应体的 8 秒期限；来源权限检查与下载共同使用一次捕获 60 秒的图片获取总预算。仅在预算仍有剩余时才会开始原生导入；一旦开始，Clipper 会等待原生主机自身的响应期限，以保留已成功保存的资源。被阻止、超时或不受支持的图片会标记为缺失，可用正文仍作为不完整捕获保留。图片压缩数据上限为 12 MiB，解码栅格须满足保守的 64 MiB 预算。超限图片会被拒绝，不会静默缩小；进程总内存可能超过该栅格预算。已有收藏和文章笔记保持不变。

## 更新

先前发布的版本没有设置迁移辅助工具。首次禁用前，请将最新源码克隆到临时目录，并保留当前控件设置。然后更新插件检出、重新构建和安装配套组件，最后重新启用控件。仅更新插件不会替换已安装的原生可执行文件，也不会重新加载浏览器扩展。

```sh
MIGRATION_SOURCE="$(mktemp -d)" &&
git clone --depth 1 https://github.com/Lcryolite/omarchy-clipper "$MIGRATION_SOURCE" &&
python3 "$MIGRATION_SOURCE/scripts/install.py" --migrate-settings &&
omarchy plugin disable lcryolite.clipper &&
omarchy plugin update lcryolite.clipper &&
cd "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/lcryolite.clipper" &&
make -j2 all &&
npm ci --ignore-scripts &&
make extension &&
npm run typecheck &&
python3 scripts/install.py \
  --chromium-extension-id CHROMIUM_EXTENSION_ID \
  --brave-extension-id BRAVE_EXTENSION_ID \
  --dry-run &&
python3 scripts/install.py \
  --chromium-extension-id CHROMIUM_EXTENSION_ID \
  --brave-extension-id BRAVE_EXTENSION_ID &&
omarchy plugin enable lcryolite.clipper &&
omarchy restart shell
```

如果克隆或迁移失败，命令链会停止，旧控件仍保持启用。最后的 shell 重启会载入更新后的面板；状态栏会短暂消失，桌面应用与收藏保持不变。重新构建后，请重新加载各个已解压的浏览器扩展。重复安装配套组件会更新两个受管理程序及其他受管理文件，同时保留持久设置和收藏。

如果迁移提示未找到旧版 Clipper 设置，请勿禁用旧控件。请恢复设置备份，或使用刚克隆到 `MIGRATION_SOURCE` 的辅助工具写入你明确知道的值，然后重试迁移：

```sh
python3 "$MIGRATION_SOURCE/clipper-settings.py" set dataDir '"/absolute/path/to/collection"'
python3 "$MIGRATION_SOURCE/clipper-settings.py" set executable '"/absolute/path/to/clipper"'
python3 "$MIGRATION_SOURCE/scripts/install.py" --migrate-settings
```

如果某个值未知，请省略对应的 `set` 命令，改从备份恢复。Clipper 不会搜索私有文件系统位置或猜测已丢失的设置。

## 卸载

在检出目录仍存在时，移除两个配套程序和浏览器注册：

```sh
cd "${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/lcryolite.clipper" &&
omarchy plugin disable lcryolite.clipper &&
python3 scripts/uninstall.py --dry-run &&
python3 scripts/uninstall.py
```

普通卸载和移除插件会保留持久设置与收藏。如需永久删除收藏，请明确指定准确的收藏路径，并在移除插件检出前检查计划：

```sh
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-clipper" &&
python3 scripts/uninstall.py --purge-data-dir "$DATA_DIR" --dry-run &&
python3 scripts/uninstall.py --purge-data-dir "$DATA_DIR"
```

清除操作不可逆。目标必须是预期的收藏目录，不会删除共享的父目录。

保留收藏或明确清除完成后，移除插件检出：

```sh
cd .. &&
omarchy plugin remove lcryolite.clipper
```

## 故障排查

- **控件能打开，但请求失败：**确认已构建并安装配套程序，`clipper` 位于 shell 的可执行文件搜索路径中，配置的数据目录可写。只有插件检出并不构成完整安装。
- **所选收藏目录不可用：**Clipper 会显示错误，不会改为打开默认收藏。请恢复所选目录的访问权限，或按上文将 `dataDir` 明确设置为预期路径或默认值。
- **无法保存设置：**确认 `${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-clipper` 可写，然后重试修改。系统会继续使用上次有效的已保存设置。
- **迁移前设置已经丢失：**随旧状态栏条目一同移除的设置无法通过猜测私有文件系统位置来重建。请保持旧控件启用，恢复设置备份，或按“更新”一节使用刚克隆的 `MIGRATION_SOURCE/clipper-settings.py` 明确设置已知的 `dataDir` 与 `executable` 后重试迁移；现有收藏内容仍保留在原目录中。
- **浏览器捕获无法连接 Clipper：**确认已解压扩展的 ID 与该浏览器的原生宿主注册一致，然后重新加载扩展。
- **更新后面板仍像旧版：**检查已安装的检出及 `qs log -p /usr/share/omarchy/shell --tail 100`。使用 `omarchy restart shell` 可重新加载桌面 shell，状态栏会短暂中断。

## 参与贡献

请在[公开问题追踪器](https://github.com/Lcryolite/omarchy-clipper/issues)报告可复现问题，附上 Omarchy、Quickshell 和 Qt 版本、状态栏位置、显示器缩放及复现步骤。请隐去日志和截图中的私人收藏内容与凭据。

无需安装即可检查源码变更：

```sh
git clone https://github.com/Lcryolite/omarchy-clipper
cd omarchy-clipper
npm ci --ignore-scripts
make -j2 all extension typecheck
omarchy plugin validate .
test "$(build/clipper --version)" = "0.1.0"
test "$(build/clipper-native --version)" = "0.1.0"
```

行为实验请使用临时 `--data-dir`，不要对日常收藏执行破坏性测试。公开源码包不包含开发仓库的测试套件。

## 许可证

Copyright 2026 Lcryolite.

Omarchy Clipper 原创源码和项目图标采用 Apache License, Version 2.0（`Apache-2.0`）。完整条款见 [LICENSE](LICENSE)。

第三方依赖保留各自许可证，归属与许可证标识记录在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 中，其中也记录了项目图标由 AI 辅助创作的来源信息。
