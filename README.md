# 一阅（Oneki reviewer）

按你设置的规则，把符合条件的“良好”自动改成“简单”。答题超过时间限制时，也可以自动调整为“困难”或“重来”，让卡片按 Anki 的排程再次出现。

支持 Anki 26.9.3 桌面版（macOS 和 Windows）。

## 安装

在 Anki 中打开 **工具 → 插件 → 获取插件**，输入插件代码：**465284797**。也可以前往[一阅的 AnkiWeb 页面](https://ankiweb.net/shared/info/465284797)安装。

安装后重启 Anki。打开 **工具 → 一阅（Oneki reviewer）** 即可进入设置。

也可以从 [GitHub Release 下载安装包](https://github.com/komakizhu/oneki-reviewer/releases/download/v0.10.2/oneki-reviewer-0.10.2.ankiaddon)，再通过 Anki 插件管理器的“从文件安装”进行安装。

## 开始使用

在“一阅”设置中启用插件，并按需打开“自动变简单”或“自动再复习”。设置完成后点击“保存”。这两项可以单独启用。

使用自动规则前，请先在 Anki「设置 → 复习」中开启「将空格键（或回车键）用于评估卡片」。之后，只有显示答案后按空格键评分时规则才会生效；点击评分按钮或使用其他评分快捷键时，插件会保留你的评分。

### 自动变简单

插件只考虑符合条件的“良好”答题。你可以选择一种判断方式：

- **固定模式**：设置答题用时上限。答得够快时，当场把“良好”改为“简单”；计时可从显示题目或显示答案开始。
- **比例模式**：设置本轮复习中最快的一部分卡片；复习结束后，符合条件的卡片会改为“简单”。
- **智能模式**：复习结束后，自动挑出本轮中答得明显更快的卡片，并改为“简单”。

“变简单保护”会检查最近的通过记录。近期出现过“困难”或“重来”时，这张卡不会自动变简单。你也可以设置每天自动变简单的上限。

### 自动再复习

答题时间达到上限后，插件会根据 Anki 当天的排程，把“良好”调整为“困难”或“重来”。

触发时间可使用 Anki 的答题时间上限：在对应牌组的“选项”中找到“计时器”，设置“回答时间记录上限（秒）”；也可以在一阅设置中选择自定义秒数。如果“自动变简单”和“自动再复习”同时符合条件，可以设置哪一项优先。

## 撤销与答题记录

固定模式即时改分后，可以点击“撤销改分”或按快捷键 **Z**（默认设置），恢复你刚选的评分和排程，同时保留这次答题记录。比例模式和智能模式会在本轮复习结束后批量处理，并显示调整张数；一阅目前不提供批量改分的撤销入口。

设置中的“最近100张答题计时”可以查看最近的答题时间和最终评分。计时、复习统计和设置保存在当前 Anki 用户资料中，不会发送到外部服务。

## 帮助与许可

遇到问题或想查看源代码，请访问[一阅 GitHub 仓库](https://github.com/komakizhu/oneki-reviewer)。

本插件采用 [GNU AGPL v3.0](LICENSE) 许可。

---

# English

Oneki reviewer automatically changes eligible **Good** ratings to **Easy** based on your rules. If an answer takes too long, it can change **Good** to **Hard** or **Again**, allowing Anki to schedule the card for another review.

Supports Anki 26.9.3 desktop on macOS and Windows.

## Install

In Anki, choose **Tools → Add-ons → Get Add-ons** and enter **465284797**. You can also install it from the [Oneki reviewer AnkiWeb page](https://ankiweb.net/shared/info/465284797).

Restart Anki after installation. Open the settings from **Tools → Oneki reviewer**.

Alternatively, [download the installer from GitHub Releases](https://github.com/komakizhu/oneki-reviewer/releases/download/v0.10.2/oneki-reviewer-0.10.2.ankiaddon) and install it through Anki’s Add-ons window using **Install from file**.

## Get started

Enable the add-on in its settings, then turn on **Automatically rate Good as Easy**, **Automatic re-review**, or both. Save your changes.

Before using automatic rules, enable **Spacebar (or enter) also answers card** in Anki’s **Preferences → Review**. Rules only run when you reveal the answer and press **Space** to rate it; clicking a rating button or using another shortcut keeps your selected rating.

### Automatically rate Good as Easy

The add-on only considers eligible **Good** answers. Choose one mode:

- **Fixed mode**: Set a time limit. A fast enough answer is changed to **Easy** immediately. You can start the timer when the question or answer appears.
- **Percentage mode**: Choose a percentage of the fastest cards in the review session. Eligible cards are changed to **Easy** when the session ends.
- **Smart mode**: At the end of the session, the add-on selects cards answered noticeably faster than the rest and changes them to **Easy**.

**Easy rating protection** checks recent passing reviews. If a recent review included **Hard** or **Again**, the card will not be changed to **Easy**. You can also set a daily limit.

### Automatic re-review

When an answer reaches the time limit, the add-on changes **Good** to **Hard** or **Again** based on Anki’s schedule for that day.

To use Anki’s answer-time limit, open the deck’s **Options**, find **Timers**, and set **Maximum answer seconds**. You can also choose a custom limit in Oneki reviewer’s settings. If both rules match, choose which one takes priority.

## Undo and review history

After an immediate change in Fixed mode, click **Undo rating change** or press **Z** (the default shortcut) to restore your selected rating and schedule while keeping the review record. Percentage and Smart modes make batch changes when the review session ends and show how many cards changed; Oneki reviewer does not currently provide an undo action for batch changes.

Open **Last 100 Review Timings** in the settings to see recent answer times and final ratings. Timing data, review statistics, and settings are stored in the active Anki profile and are not sent to an external service.

## Help and license

Visit the [Oneki reviewer GitHub repository](https://github.com/komakizhu/oneki-reviewer) for source code or to report an issue.

Licensed under [GNU AGPL v3.0](LICENSE).
