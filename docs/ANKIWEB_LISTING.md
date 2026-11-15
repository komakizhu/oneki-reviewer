# AnkiWeb 上架文案 / AnkiWeb listing copy

## 名称 / Name

一阅（Oneki reviewer）

## 简介 / Summary

**中文：** 按可配置的复习规则，将符合条件的“良好”自动调整为“简单”，并记录答题时间与撤销信息。

**English:** Automatically changes eligible Good ratings to Easy using configurable review rules, with answer timing and undo support.

## 详细说明 / Description

**中文**

一阅为 Anki 桌面版提供自动评分辅助。你可以按答题速度、通过记录、复习批次比例或智能规则，决定哪些“良好”可以自动变成“简单”。插件保留原有答题次数和复习记录，并提供即时撤销入口。设置可在“工具 → 一阅（Oneki reviewer）”中打开。

支持 Anki 2.1.0 及更新的 macOS 和 Windows 桌面版。使用旧排程器时（包括 Anki 2.1.0–2.1.49），固定计时规则可用；自动再复习会选“重来”，以确保当天重新出现。比例、智能批次、每日上限和批量改分需要 V3 排程器。当前全功能路径已在 Anki 26.9.0–26.9.4 上验证。答题计时、复习统计和设置保存在当前 Anki profile 中，不会发送到外部服务。插件采用 GNU AGPL v3.0。

**English**

Oneki reviewer adds configurable automatic rating assistance to the Anki desktop app. Rules can use answer speed, recent review history, batch ratios, or adaptive criteria to decide which Good ratings become Easy. The add-on keeps the original review count and history and provides an immediate undo action. Open its settings from **Tools → Oneki reviewer**.

Supports Anki 2.1.0 and newer on macOS and Windows. With the classic scheduler, including Anki 2.1.0–2.1.49, fixed timing rules are available; automatic re-review uses Again so the card is shown again that day. Percentage and smart batch selection, daily limits, and batch regrading require the V3 scheduler. The full feature path has been verified with Anki 26.9.0–26.9.4. Answer timing, review statistics, and settings are stored in the active Anki profile and are not sent to an external service. Licensed under GNU AGPL v3.0.

## 安装与使用 / Installation and use

在 Anki 中通过 **工具 → 插件 → 获取插件** 输入 AnkiWeb 页面提供的插件代码；安装后重启 Anki，点击 **工具 → 一阅（Oneki reviewer）** 打开设置。

In Anki, choose **Tools → Add-ons → Get Add-ons**, enter the code shown on the AnkiWeb listing, and restart Anki. Then click **Tools → Oneki reviewer** to open settings.

## 支持 / Support

- License: GNU AGPL v3.0, see [LICENSE](../LICENSE).
- Source and issue tracker: <https://github.com/komakizhu/oneki-reviewer>
- Compatibility: Anki 2.1.0 and newer desktop versions on macOS and Windows; see README for classic scheduler limitations.
- AnkiWeb add-on code: `465284797`
- AnkiWeb listing: <https://ankiweb.net/shared/info/465284797>
