# Bug 修复记录

## 2026-11-09 知网点字验证码改用“请依次单击”

- **现象**。知网详情页显示“请依次单击【…】”和点字图片，命令直接报告 `captcha skipped`，日志里没有 `captcha char scores`。将 `CNKI_MIN_CHAR_SCORE` 和 `CNKI_SLIDER_MIN_CONF` 都设为 0 后仍可复现。
- **原因**。`scripts/cnki_captcha.py` 的 `FIND_JS` 只匹配 `/依次点击/`。遇到“依次单击”时，`msg` 为空，`_verify_page()` 无法交给 OCR 处理。
- **修复**。将提示识别正则改成 `/依次(?:点击|单击)/`，兼容新旧文案。
- **验证**。在 macOS 的知网专用 Chrome 会话中，通过 `cnki_detail.py` 重试此前失败的文献。默认阈值下，日志恢复输出字符分数，第二张图通过并取得摘要。该批次最终补齐 7 篇摘要，并下载、核验 4 份 PDF。
- **Windows 移植**。在移植版的浏览器 DOM 提示识别处同步这一行 JavaScript 正则。用“请依次单击【…】”和“请依次点击【…】”各验证一次，预期均可提取题面并进入点字识别。Windows 实测待完成。
