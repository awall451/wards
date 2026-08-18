#!/usr/bin/env bash
# ward-tamper-guard.py — the agent may not edit its own wards.
# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
H=ward-tamper-guard.py
P="$SCRATCH/proj"; mkdir -p "$P/.claude/hooks" "$P/.wards" "$P/src"
okf() { expect "allow edit: $1" 0 $H "$(ev PreToolUse Edit "$1" s1)"; }
nof() { expect "block edit: $1" 2 $H "$(ev PreToolUse Edit "$1" s1)"; }
okb() { expect "allow bash: ${1:0:60}" 0 $H "$(bash_ev "$1" s1)"; }
nob() { expect "block bash: ${1:0:60}" 2 $H "$(bash_ev "$1" s1)"; }

echo "# file tools"
nof "$P/.claude/settings.json"
nof "$P/.claude/settings.local.json"
nof "$P/.claude/hooks/ward-lint.py"
nof "$P/.wards/config.toml"
nof "$P/.wards/complexity-allow.txt"
nof "$HOME/.claude/settings.json"
expect "Write to hooks dir blocked" 2 $H "$(ev PreToolUse Write "$P/.claude/hooks/new-hook.sh" s1)"
okf "$P/src/app.py"
okf "$P/.claude/CLAUDE.md"
okf "$P/claude/hooks/ward-lint.py"        # the wards SOURCE tree (no leading dot) is a normal repo
okf "$P/wards-notes.md"
expect "PostToolUse ignored" 0 $H "$(ev PostToolUse Edit "$P/.wards/config.toml" s1)"

echo "# bash writes"
nob "echo '{}' > .claude/settings.json"
nob "cat >> .wards/config.toml <<EOF
[lint]
enabled = false
EOF"
nob "rm -f .claude/hooks/ward-complexity.py"
nob "mv .claude/hooks/ward-lint.py /tmp/"
nob "sed -i 's/true/false/' .wards/config.toml"
nob "chmod -x .claude/hooks/ward-git-guard.py"
nob "cp /dev/null $P/.claude/hooks/ward-tests.py"
nob "python3 -c \"open('.wards/config.toml','w').write('')\""
nob "tee .claude/settings.local.json < x.json"
nob "git checkout -- .claude/settings.json"
nob "git rm .claude/hooks/ward-lint.py"
nob "cp settings.json ~/.claude/settings.json"
nob "echo x > \$HOME/.claude/settings.json"

echo "# red-team bypasses (allow-list model)"
nob 'd=".wa""rds"; echo enabled=false > $d/config.toml'      # quote-splitting normalised
nob "ed -s .wards/config.toml"
nob "ex -s -c wq .wards/config.toml"
nob "vim -Es .claude/hooks/ward-lint.py"
nob "gawk -i inplace '{print}' .wards/config.toml"
nob "python3 writer.py .wards/config.toml"
nob "python3 -m mytool .claude/settings.json"
nob "node fix.js .claude/hooks/x.py"
nob "perl -pi -e 's/a/b/' .wards/config.toml"
nob "xargs rm < list-of-.claude/hooks-files"
nob "find .claude/hooks -name '*.py' -delete"
nob "find .wards -exec rm {} \\;"
nob "install -m 644 x .claude/settings.json"
nob "rm -rf .git"
nob "mv .git .git.bak"
nob "rm .git/hooks/pre-commit"
nob "chmod -x .git/hooks/pre-commit"
nob "pre-commit uninstall"
nob "git config core.hooksPath /dev/null"
nob "git -c core.hooksPath=/tmp/x commit -m 'feat: x'"
nob "cat x > .pre-commit-config.yaml"
nob "sed --in-place 's/x/y/' .wards/config.toml"
nob "awk '{print}' .wards/config.toml > .wards/config.toml.new"
okb "find .claude/hooks -name '*.py'"
okb "awk '{print}' .wards/config.toml"
okb "sed 's/x/y/' .wards/config.toml"
okb "git log -- .claude/settings.json"
okb "git show HEAD:.wards/config.toml"
okb "cat .git/config"
okb "python3 -c 'print(1)' > out.txt"                        # unrelated write
okb "wc -l .wards/config.toml .claude/hooks/*.py"
okb "sudo cat .claude/settings.json"
okb "git config --get core.hooksPath"
nob "git config --local core.hooksPath /dev/null"
okb "git add .claude/hooks/ward-lint.py .wards/config.toml"       # staging a human's change is not a write
okb "git add -A -- .claude/hooks"
okb "git commit -m 'chore: update hooks' -- .claude/settings.json"
okb "git push origin main"
okb "git stash pop"                                                # no protected path named -> not tamper's domain
nob "git checkout -- .claude/hooks/ward-lint.py"
nob "git restore .wards/config.toml"
nob "git rm .claude/hooks/ward-lint.py"

echo "# bash reads and unrelated writes pass"
okb "cat .claude/settings.json"
okb "ls -la .claude/hooks/"
okb "grep -n enabled .wards/config.toml"
okb "diff .wards/config.toml other.toml"
okb "git diff .claude/settings.json"
okb "jq .hooks .claude/settings.json 2>&1 | head"
okb "echo done > out.txt"
okb "rm -rf build/"
okb "cat .wards/config.toml 2>&1"

echo "# config disable (human, e.g. in the wards dev repo)"
printf '[tamper]\nenabled = false\n' > "$P/.wards/config.toml"
cd "$P" || exit 1
expect "disabled -> edit allowed" 0 $H "$(ev PreToolUse Edit "$P/.claude/settings.json" s1)"
cd "$SCRATCH" || exit 1
rm "$P/.wards/config.toml"
finish
