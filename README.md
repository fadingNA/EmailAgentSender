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
