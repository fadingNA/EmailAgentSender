# EmailAgentSender

Send a daily Ollama-generated cybersecurity news digest by email.

## MailerSend

Set these environment variables in `.env` to send with MailerSend:

```sh
MAIL_PROVIDER=mailersend
MAILERSEND_API_KEY=your_api_token
MAILERSEND_FROM=news@yourdomain.com
MAILERSEND_FROM_NAME="OpenFang News"
MAILERSEND_TO=person@example.com,team@example.com
MAILERSEND_USER_AGENT=openfang-news/1.0
```

`MAILERSEND_TO` falls back to `SMTP_TO`, and `MAILERSEND_FROM` falls back to `SMTP_FROM`.

MailerSend requires `MAILERSEND_FROM` to use a verified sending domain. If the
account is still in trial mode, keep `MAILERSEND_TO` to a recipient allowed by
your MailerSend account.

## App Vendor Focus

Place CSV files under `data/` with an `app_vendor` column. The digest job reads
that column, removes duplicate vendor names, and adds the vendor list to the
Ollama web-search prompt so the briefing can look for vendor-specific
cybersecurity news, advisories, breaches, and vulnerabilities.

Optional environment variables:

```sh
APP_VENDOR_DATA_DIR=data
APP_VENDOR_COLUMN=app_vendor
APP_VENDOR_LIMIT=50
APP_VENDOR_CACHE_PATH=data/.cache/app_vendors.json
GENERAL_SECURITY_MAX_ITEMS=15
WEB_SEARCH_RESULT_LIMIT=65
```

Preview the vendor list with:

```sh
python3 component/app_vendors.py
```

The vendor list is cached so the mail job does not rescan CSV files every run.
Use these flags when the CSV data changes:

```sh
python3 component/app_vendors.py --refresh-cache
python3 send_mail.py --refresh-app-vendors
```
