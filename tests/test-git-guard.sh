#!/usr/bin/env bash
# ward-git-guard.py — blocked git verbs, commit-message hygiene, segment inspection.
# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
H=ward-git-guard.py
ok()  { local l="${1//$'\n'/ }"; expect "allow: ${l:0:70}" 0 $H "$(bash_ev "$1" s1)"; not_asked "  (no prompt)"; }
ask() { local l="${1//$'\n'/ }"; expect "ask: ${l:0:70}" 0 $H "$(bash_ev "$1" s1)"; expect_out "  (prompts the human)" '"permissionDecision": "ask"'; }
not_asked() { if grep -q '"permissionDecision": "ask"' <<<"$LAST_OUT"; then FAIL=$((FAIL+1)); printf '  FAIL %s\n%s\n' "$1" "$LAST_OUT"; else PASS=$((PASS+1)); fi; }
no()  { local l="${1//$'\n'/ }"; expect "block: ${l:0:70}" 2 $H "$(bash_ev "$1" s1)"; }

echo "# reads and ordinary work pass"
ok "git status"
ok "git log --oneline | head"
ok "git add -A && git commit -m 'feat(hooks): add git guard'"
ok "git commit -m \"fix: handle empty payload\" -m \"body text\""
ok "git commit -am 'chore: tidy'"
ok "git commit -m 'refactor!: drop legacy api'"
ok "git push origin feature/x"
ok "git push -n origin main"                       # dry-run, not no-verify
ok "git reset --soft HEAD~1"
ok "git reset HEAD file.txt"
ok "git checkout -b feature/x"
ok "git checkout main"
ok "git restore --staged file.txt"
ok "git restore file.txt"
ok "git stash && git stash pop"
ok "git branch -d merged-branch"
ok "git clean -n"
ok "git config user.email dillon@sigilworks.dev"
ok "git config --local user.name 'Dillon Hartline'"
ok "git remote -v && git remote add github https://example"
ok "git rebase main"
ok "git -C /some/repo commit -m 'docs: readme'"
ok "grep -r 'git push' notes.md"                  # not a git invocation
ok "ls; cat file"

echo "# hook bypass"
no "git commit --no-verify -m 'feat: x'"
no "git commit -n -m 'feat: x'"
no "git commit -am 'feat: x' --no-verify"
no "git push --no-verify"

echo "# history rewrite"
no "git push --force origin main"
no "git push -f origin main"
no "git push --force-with-lease origin main"
no "git push --force-with-lease=main:abc origin"
no "git filter-branch --tree-filter 'rm -f secrets' HEAD"
no "git filter-repo --path secrets --invert-paths"
no "git reflog expire --expire=now --all"
no "git gc --prune=now"
no "git update-ref -d refs/heads/x"

echo "# work destruction"
no "git reset --hard HEAD~3"
no "git reset --hard"
no "git clean -fd"
no "git clean -f"
no "git clean --force -x"
no "git checkout -- ."
no "git checkout ."
no "git restore ."
no "git stash drop"
no "git stash clear"
no "git branch -D feature/x"
no "git push origin --delete feature/x"
no "git push origin :feature/x"

echo "# config / identity"
no "git config --global user.email x@y"
no "git config --system core.editor vim"
no "git remote remove origin"
no "git remote rm github"
no "git remote set-url origin https://evil"

echo "# smuggling past a read"
no "git status && git push --force"
no "git log | head; git reset --hard"
no "cd repo && git clean -fd"

echo "# conventional commits"
no "git commit -m 'Add git guard'"
no "git commit -m 'added stuff'"
no "git commit -m 'feat add thing'"
no "git commit -m 'feat: $(printf 'x%.0s' {1..80})'"
expect "long subject message" 2 $H "$(bash_ev "git commit -m 'feat: $(printf 'x%.0s' {1..80})'" s1)"
expect_out "says <= 72" '<= 72'
ok "git commit -F msg.txt"
ok "git commit"
mkdir -p "$SCRATCH/.wards"; printf '[git]\nconventional_commits = false\n' > "$SCRATCH/.wards/config.toml"
ok "git commit -m 'whatever i want'"
printf '[git]\ntypes = ["feat","fix","wip"]\n' > "$SCRATCH/.wards/config.toml"
ok "git commit -m 'wip: half done'"
no "git commit -m 'chore: not in custom types'"
rm "$SCRATCH/.wards/config.toml"

echo "# heredoc / \$(…) commit messages (Claude Code's own style)"
HD='git commit -m "$(cat <<'"'"'EOF'"'"'
feat(hooks): add git guard

Body line with & and | and ; inside.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"'
expect "heredoc CC message allowed"        0 $H "$(bash_ev "$HD" s1)"
HD_BAD='git commit -m "$(cat <<'"'"'EOF'"'"'
Added the git guard

details
EOF
)"'
expect "heredoc non-CC message blocked"   2 $H "$(bash_ev "$HD_BAD" s1)"
expect "-F - heredoc CC allowed"          0 $H "$(bash_ev $'git commit -F - <<\'EOF\'\nfix: thing\n\nbody\nEOF' s1)"
expect "-F - heredoc non-CC blocked"      2 $H "$(bash_ev $'git commit -F - <<\'EOF\'\nthing fixed\nEOF' s1)"
expect "unevaluable \$(…) -> allowed"     0 $H "$(bash_ev 'git commit -m "$(python3 gen.py)"' s1)"
expect "message containing & not split"   0 $H "$(bash_ev "git commit -m 'feat: a & b | c; d'" s1)"
expect "message with & then force push"   2 $H "$(bash_ev "git commit -m 'feat: a & b' && git push --force" s1)"
expect "push +refspec force -> block"     2 $H "$(bash_ev "git push origin +main" s1)"
expect "push refspec no plus -> ok"       0 $H "$(bash_ev "git push origin feat:feat" s1)"
expect "quoted git in grep not parsed"    0 $H "$(bash_ev "grep -rn 'git push --force' docs/" s1)"
expect "git in path (/usr/bin/git)"       2 $H "$(bash_ev "/usr/bin/git reset --hard" s1)"
expect "-C and -c global opts"            2 $H "$(bash_ev "git -C /repo -c user.name=x push -f" s1)"
expect "unbalanced quote degrades safely" 2 $H "$(bash_ev "git push --force 'oops" s1)"
expect "heredoc BODY mentioning git ignored" 0 $H "$(bash_ev $'cat > notes.md <<\'EOF\'\nnever run git push --force or git reset --hard\nEOF' s1)"
expect "python heredoc mentioning git ignored" 0 $H "$(bash_ev $'python3 - <<\'EOF\'\nprint("git clean -fd")\nEOF' s1)"
expect "heredoc then real force push still caught" 2 $H "$(bash_ev $'cat > x <<\'EOF\'\nhello\nEOF\ngit push --force' s1)"

echo "# reviewer findings: separators, redirects, abbreviations, wrappers"
no $'git status\ngit push --force'                       # newline is a separator
no $'git status # note\ngit commit -m "bad message"'
no $'git status\ngit commit -m "wip"'
no "git restore . 2>&1"
no "git checkout . 2>/dev/null"
no "git checkout -- . >/dev/null"
no "(git push -f)"
no 'x=$(git push -f)'
no 'echo "$(git push --force)"'
no "\`git push --force\`"
no "git push --forc origin main"                          # git accepts unambiguous prefixes
no "git reset --har"
no "git commit -m 'feat: x' --no-veri"
no "git branch -df x"
no "git branch -d -f x"
no "git branch --dele --forc x"
no "git branch -M main"
no "git branch -f main HEAD~5"
no "sh -c 'git push --force'"
no 'bash -lc "git reset --hard"'
no 'eval "git push --force"'
no '"git" push --force'
no "\\git push --force"
no 'g""it push --force'
no "git push --force\$IFS origin main"
no "python3 -c \"import subprocess;subprocess.run(['git','push','--force'])\""
no 'node -e "require(\"child_process\").execSync(\"git push --force\")"'
no "git -c core.hooksPath=/dev/null commit -m 'feat: x'"
no "git config core.hooksPath /dev/null"
no "git config alias.fp 'push --force'"
no "git -c alias.zz='push --force' zz"
no "git rm -rf tests/"
no "git rm -r --cached ."
no "git checkout -f main"
no "git switch -C main"
no "git switch --discard-changes"
no "git checkout HEAD ."
no "git checkout ./"
no "git checkout -- src/"
no "git restore src/"
no "git restore ./"
no "git restore -s HEAD~5 ."
no "git update-ref --delete refs/heads/x"
no "git update-ref refs/heads/main HEAD~5"
no "git worktree remove -f wt"
no "git worktree prune"
no "git remote rename origin old"
no "git reflog delete HEAD@{1}"
no "git prune --expire=now"
no "git-filter-repo --path secrets --invert-paths"
no "python3 -m git_filter_repo --path x"
no "git rebase -i HEAD~3"
no "git rebase --root"
no "git push --mirror origin"
no "git push --prune origin"
no "git commit -am'bad message'"
no "git commit -ambad"
no "echo 'bad message' | git commit -F -"
no $'true <<<"EOF"\ngit push --force\nEOF'              # here-string is not a heredoc opener
ok "git commit -m 'feat: x' -m '-n'"                     # message body is not a flag
ok "git commit -m 'feat: x' -m '--no-verify'"
ok 'git commit -m "$MSG"'                                # unknowable
ok 'git commit -m "${MSG}"'
ok "git commit -m \"Merge branch 'x' into main\""
ok "git commit -m 'Revert \"feat: x\"'"
ok "git commit -m 'fixup! feat: x'"
ok "git commit -m 'Initial commit'"
ok "git config --global --list"
ok "git config --global --get user.name"
ok "git config --get-regexp alias"
ok "git config --get alias.pushall"
ok "git config --get core.hooksPath"
ok "git pushall"
ok "git checkout -- specific/file.py"
ok "git restore file.py"
ok "git checkout main"
ok "git checkout -b feature/x"
ok "git rm old_file.py"
ok "git worktree add ../wt"
ok "git worktree remove wt"
ok "git remote add github https://example"
ok "git rebase main"
ok "git commit --amend --no-edit"
ok "git commit --fixup HEAD"
ok "grep -rn 'git push --force' docs/"
ok "echo 'git reset --hard is bad'"
ok "git log --grep='reset --hard'"
ok "git commit -m 'feat: msg' 2>&1 | tail -1"
ok $'cat > notes.md <<\'EOF\'\nnotes\nEOF\ngit commit -m "$(cat <<\'EOF\'\ndocs: add notes\nEOF\n)"'   # nearest heredoc is the message
echo "# gates: protected-branch push and PR merge ask the human (exit 0 + ask decision)"
git init -q -b main . && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m "chore: init"
git config alias.pushall '!git push origin && git push github'
git config alias.pm 'push origin main'
ask "git push origin main"
ask "git push origin HEAD:refs/heads/main"
ask "git push origin feat:main"
ask "git push"                                       # current branch is main
ask "git push origin HEAD"
ask "git push --all origin"
ask "git pushall"                                    # alias expanding to pushes of main
ask "git pm"
ask "git add -A && git commit -m 'feat: x' && git push origin main"
ask "gh pr merge 12 --squash"
ask "az repos pr update --id 5 --status completed"
ask "az repos pr create --source-branch x --auto-complete true"
ask "curl -X PATCH https://dev.azure.com/o/p/_apis/git/repositories/r/pullrequests/5 -d '{\"status\":\"completed\"}'"
ok "git push -n origin main"                         # dry run
ok "gh pr create --fill"
ok "az repos pr create --source-branch x --target-branch main"
ok "gh pr view 12"
no "git push --force origin main"                    # never-tier wins over the gate
git switch -q -c feat/y
ok "git push"                                        # current branch is not protected
ok "git push -u origin feat/y"
ok "git pushall"                                     # alias pushes feat/y
expect "browser js: PR completion asks" 0 $H "$(jq -cn --arg t "await fetch('/o/p/_apis/git/repositories/q/pullrequests/5?api-version=7.1',{method:'PATCH',body:JSON.stringify({status:'completed'})})" '{hook_event_name:"PreToolUse",tool_name:"mcp__claude-in-chrome__javascript_tool",session_id:"s1",tool_input:{action:"javascript_exec",text:$t}}')"
expect_out "  (prompts the human)" '"permissionDecision": "ask"'
expect "browser js: PR creation passes" 0 $H "$(jq -cn --arg t "await fetch('/o/p/_apis/git/repositories/q/pullrequests?api-version=7.1',{method:'POST'})" '{hook_event_name:"PreToolUse",tool_name:"mcp__claude-in-chrome__javascript_tool",session_id:"s1",tool_input:{action:"javascript_exec",text:$t}}')"
not_asked "  (no prompt)"
mkdir -p .wards && printf '[git]\nprotected_push = "block"\nprotected_branches = ["release"]\n' > .wards/config.toml
no "git push origin release"
ok "git push origin main"                            # main not protected in this config
printf '[git]\nprotected_push = "allow"\npr_merge = "allow"\n' > .wards/config.toml
ok "git push origin main"
ok "gh pr merge 12"
rm -rf .wards
echo "# audit"
check "audit has ASKED" grep -q "ASKED: git" "$WARDS_AUDIT_LOG"
check "audit has BLOCKED" grep -q "BLOCKED: git" "$WARDS_AUDIT_LOG"
check "audit has ALLOWED" grep -q "ALLOWED: git" "$WARDS_AUDIT_LOG"
check_not() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then FAIL=$((FAIL+1)); printf '  FAIL %s\n' "$label"; else PASS=$((PASS+1)); printf '  ok   %s\n' "$label"; fi; }
check_not "reads not logged" grep -q "git status$" "$WARDS_AUDIT_LOG"
finish
