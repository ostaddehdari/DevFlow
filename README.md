# DevFlow

AI Development, Deployment & Continuity Platform.

## وضعیت فعلی

- Stage 0: قراردادهای JSON و سورس افزونهٔ مهارت‌ها ثبت شده‌اند.
- Stage 1: رجیستری کنترل و ServerMaster MCP با انتقال `stdio` پیاده‌سازی شده‌اند.
- نصب و تأیید روی سرور کنترل باید با دستور زیر انجام شود؛ وجود سورس به معنی استقرار نیست.

## نصب و بررسی Stage 1

روی Ubuntu 24.04 با Python 3.12 و Git، داخل همین مخزن:

```bash
cd /opt/devflow
bash scripts/bootstrap-stage1.sh
```

اسکریپت وابستگی‌های نسخه‌بندی‌شده را در `.venv` نصب می‌کند، آزمون‌ها را اجرا می‌کند،
DevFlow و سرور تنظیم‌شده در `.ai-project/project.settings.json` را ثبت می‌کند و ارتباط واقعی
MCP را با یک کلاینت موقت بررسی می‌کند. اجرای دوباره، ثبت تکراری ایجاد نمی‌کند.

```bash
bash scripts/devflow validate
bash scripts/devflow list
bash scripts/devflow status
```

رجیستری اجرایی در `control/registry.json` نگهداری می‌شود و وارد Git نمی‌شود.
برای نسخهٔ پشتیبان، این پوشه را همراه اسناد پروژه و مخزن نگه دارید.
جزئیات در [docs/STAGE-01.md](docs/STAGE-01.md) آمده است.

## ابزارهای MCP

| ابزار | کارکرد |
| --- | --- |
| `servermaster_ping` | نسخه و قابلیت‌های واقعاً پیاده‌شده |
| `registry_projects` | پروژه‌ها و پروژهٔ فعال |
| `registry_servers` | اطلاعات ثبت‌شدهٔ سرورها؛ بدون بررسی سلامت زنده |
| `registry_validate` | اعتبارسنجی قرارداد، شناسه‌های یکتا و پروژهٔ فعال |
| `project_status` | وضعیت ثبت‌شده، HEAD محلی Git و کمبود اسناد تداوم |

برای اجرا با یک کلاینت محلی MCP، نمونهٔ تنظیمات در `examples/mcp.local.json` است.
اجرای `bash scripts/devflow serve` منتظر ورودی پروتکل روی stdin می‌ماند؛
کلاینت MCP باید آن را راه‌اندازی کند. این فرمان یک وب‌سرور دائمی ایجاد نمی‌کند.

## حدود این مرحله

مرورگر جزو این پروژه نیست. Stage 1 پورت شبکه، Nginx، systemd، SSH، دیتابیس یا cron را
تغییر نمی‌دهد. اجرای فرمان روی سرور، agent، Vault، execution lease، watchdog، گزارش
تلگرام و اتصال خصوصی افزونه هنوز پیاده‌سازی یا فعال نشده‌اند؛ حتی اگر در تنظیمات درخواست شده باشند.
اسرار باید خارج از Git بمانند و با `vault://...` یا ارجاع‌های مجاز قرارداد معرفی شوند.
