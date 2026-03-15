# Security Rules — MANDATORY

- Never hardcode API keys, private keys, or webhook URLs in source code
- Never print key values in logs — mask or omit entirely
- All credentials loaded via environment variables (os.getenv)
- Never commit .env files — verify .gitignore includes .env
- Never log full API responses that might contain auth tokens
- Before any git push: grep for potential secrets in staged files
