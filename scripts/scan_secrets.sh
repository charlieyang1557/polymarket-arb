#!/bin/bash
# Scan staged files for potential secrets (hardcoded values, not env var references)
# Patterns look for actual secret VALUES, not references to env var NAMES

FOUND=0
STAGED=$(git diff --cached --name-only 2>/dev/null)

if [ -z "$STAGED" ]; then
    echo "No staged files to scan."
    exit 0
fi

# Exclude known safe files
SAFE_FILES="scripts/scan_secrets.sh .env.example .claude/agents/risk-reviewer.md"

check_pattern() {
    local pattern="$1"
    local desc="$2"
    MATCHES=$(echo "$STAGED" | xargs grep -l "$pattern" 2>/dev/null)
    if [ -n "$MATCHES" ]; then
        for f in $MATCHES; do
            local skip=0
            for safe in $SAFE_FILES; do
                if [[ "$f" == "$safe" ]]; then
                    skip=1
                    break
                fi
            done
            if [ $skip -eq 0 ]; then
                echo "WARNING: $desc in: $f"
                FOUND=1
            fi
        done
    fi
}

# Look for hardcoded secret values (not os.getenv references)
check_pattern 'BEGIN RSA PRIVATE KEY' "RSA private key"
check_pattern 'BEGIN EC PRIVATE KEY' "EC private key"
check_pattern 'BEGIN PRIVATE KEY' "Private key"
check_pattern 'whsec_[a-zA-Z0-9]' "Webhook secret"
check_pattern 'sk-ant-[a-zA-Z0-9]' "Anthropic API key"
check_pattern 'sk-proj-[a-zA-Z0-9]' "OpenAI API key"
# Hardcoded key assignments (value after = is a string literal, not getenv)
check_pattern "api_key\s*=\s*['\"][a-zA-Z0-9]" "Hardcoded API key value"
check_pattern "private_key\s*=\s*['\"][a-zA-Z0-9/]" "Hardcoded private key value"
check_pattern "webhook_url\s*=\s*['\"]http" "Hardcoded webhook URL"
# Discord webhook URLs (not env var references)
check_pattern 'https://discord.com/api/webhooks/' "Discord webhook URL"
check_pattern 'https://discordapp.com/api/webhooks/' "Discord webhook URL"

if [ $FOUND -eq 1 ]; then
    echo ""
    echo "FAILED: Secret scan found potential secrets. Remove them before committing."
    echo "   Use environment variables instead."
    exit 1
fi

echo "Secret scan passed."
exit 0
