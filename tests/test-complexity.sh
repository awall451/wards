#!/usr/bin/env bash
# ward-complexity.py — ratchet policy across ccn / length / params / file_lines.
# shellcheck source=tests/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
H=ward-complexity.py
# gen <name> <branches> [params]  -> python fn with CCN = branches+1
gen() { local ps="${3:-x}"; echo "def $1($ps):"; echo "    r=0"; for i in $(seq 1 "$2"); do echo "    if x==$i: r+=$i"; done; echo "    return r"; }
# genlong <name> <lines> -> fn with NLOC ~ lines, CCN 1
genlong() { echo "def $1(x):"; for i in $(seq 1 "$2"); do echo "    x = x + $i"; done; echo "    return x"; }
F="$SCRATCH/legacy.py"; N="$SCRATCH/new.py"

echo "# ccn ratchet"
{ gen legacy_high 30; gen legacy_vhigh 59; gen fine 3; } > "$F"
expect "pre snapshot"                    0 $H "$(ev PreToolUse Edit "$F" s1)"
expect "legacy untouched -> warn, exit 0" 0 $H "$(ev PostToolUse Edit "$F" s1)"
expect_out "warn mentions legacy_high"   'legacy_high\(\) CCN=31 \[high, legacy'
expect_out "warn is additionalContext"   'additionalContext'
expect "second touch -> warn suppressed" 0 $H "$(ev PostToolUse Edit "$F" s1)"
check "warn-once" [ -z "$LAST_OUT" ]
{ gen legacy_high 32; gen legacy_vhigh 59; gen fine 3; } > "$F"
expect "legacy grew 31->33 -> block"     2 $H "$(ev PostToolUse Edit "$F" s1)"
expect_out "block says ADDED"            'made it worse \(\+2\)'
{ gen legacy_high 24; gen legacy_vhigh 59; gen fine 3; } > "$F"
expect "legacy shrank -> allowed"        0 $H "$(ev PostToolUse Edit "$F" s1)"
{ gen legacy_high 24; gen legacy_vhigh 62; gen fine 3; } > "$F"
expect "very-high grew -> block"         2 $H "$(ev PostToolUse Edit "$F" s1)"

echo "# new code"
expect "pre on nonexistent file"         0 $H "$(ev PreToolUse Write "$N" s1)"
gen brand_new 21 > "$N"
expect "new fn CCN 22 -> block"          2 $H "$(ev PostToolUse Write "$N" s1)"
expect_out "hint present"                'extract helpers'
gen brand_new 14 > "$N"
expect "moderate CCN 15 -> silent"       0 $H "$(ev PostToolUse Write "$N" s1)"
gen brand_new 9 > "$N"
expect "low -> silent"                   0 $H "$(ev PostToolUse Write "$N" s1)"

echo "# length / params / file_lines"
genlong longboi 120 > "$N"
expect "new fn NLOC 122 -> block"        2 $H "$(ev PostToolUse Write "$N" s1)"
expect_out "length label"                'NLOC=12[0-9] \[high\]'
genlong longboi 60 > "$N"
expect "NLOC 62 moderate -> ok"          0 $H "$(ev PostToolUse Write "$N" s1)"
gen manyargs 1 "a,b,c,d,e,f,g,x" > "$N"
expect "8 params -> block"               2 $H "$(ev PostToolUse Write "$N" s1)"
gen manyargs 1 "a,b,c,d,x" > "$N"
expect "5 params moderate -> ok"         0 $H "$(ev PostToolUse Write "$N" s1)"
{ for i in $(seq 1 90); do genlong "f$i" 8; done; } > "$N"   # 90 fns * 10 lines = 900 lines
expect "new file 900 lines -> block"     2 $H "$(ev PostToolUse Write "$N" s1)"
expect_out "file label"                  '\(file\)\(\) lines=9[0-9]{2} \[high\]'

echo "# stop sweep"
gen brand_new 3 > "$N"; { gen legacy_high 24; gen legacy_vhigh 59; gen fine 3; } > "$F"
expect "stop, all clean -> 0"            0 $H "$(ev Stop '' '' s1)"
gen brand_new 25 > "$N"
expect "stop with violation -> 2"        2 $H "$(ev Stop '' '' s1)"
expect "stop_hook_active still checks"   2 $H "$(ev Stop '' '' s1 '{"stop_hook_active":true}')"
expect "3rd consecutive stop block"      2 $H "$(ev Stop '' '' s1 '{"stop_hook_active":true}')"
expect "4th -> limit hit, lets it stop"  0 $H "$(ev Stop '' '' s1 '{"stop_hook_active":true}')"
gen brand_new 3 > "$N"
expect "clean stop resets counter"       0 $H "$(ev Stop '' '' s1)"
gen brand_new 25 > "$N"
expect "blocks again after reset"        2 $H "$(ev Stop '' '' s1)"

echo "# allow-list + config + ignore + unsupported"
mkdir -p "$SCRATCH/.wards"; echo brand_new > "$SCRATCH/.wards/complexity-allow.txt"
expect "allow-listed fn -> 0"            0 $H "$(ev PostToolUse Write "$N" s1)"
rm "$SCRATCH/.wards/complexity-allow.txt"
printf '[complexity]\nccn = [10, 30, 50]\n' > "$SCRATCH/.wards/config.toml"
expect "config raises HIGH to 30 -> 0"   0 $H "$(ev PostToolUse Write "$N" s1)"
rm "$SCRATCH/.wards/config.toml"
expect "config gone -> block again"      2 $H "$(ev PostToolUse Write "$N" s1)"
mkdir -p "$SCRATCH/node_modules"; gen x 40 > "$SCRATCH/node_modules/x.py"
expect "ignored path -> 0"               0 $H "$(ev PostToolUse Write "$SCRATCH/node_modules/x.py" s1)"
echo "# md" > "$SCRATCH/x.md"
expect "unsupported filetype -> 0"       0 $H "$(ev PostToolUse Write "$SCRATCH/x.md" s1)"
expect "non-file tool ignored"           0 $H "$(ev PostToolUse Bash "$N" s1)"

echo "# subproject config wins over workspace root (anchor = edited file)"
SUB="$SCRATCH/sub"; mkdir -p "$SUB/.wards"
printf '[complexity]\nccn = [10, 30, 50]\n' > "$SUB/.wards/config.toml"
printf '[complexity]\nccn = [10, 20, 50]\n' > "$SCRATCH/.wards/config.toml"
gen subfn 25 > "$SUB/s.py"
expect "sub config (HIGH=30) applies -> 0" 0 $H "$(ev PostToolUse Write "$SUB/s.py" s5)"
gen rootfn 25 > "$SCRATCH/r.py"
expect "root config (HIGH=20) applies -> 2" 2 $H "$(ev PostToolUse Write "$SCRATCH/r.py" s5)"
rm -rf "$SUB" "$SCRATCH/r.py" "$SCRATCH/.wards/config.toml"

echo "# reviewer findings"
# self/cls not counted as params
printf 'class A:\n    def m(self, a, b, c, d, e, f):\n        return a\n' > "$N"
expect "method with self + 6 params -> ok"  0 $H "$(ev PostToolUse Write "$N" s7)"
printf 'class A:\n    def m(self, a, b, c, d, e, f, g):\n        return a\n' > "$N"
expect "method with self + 7 params -> block" 2 $H "$(ev PostToolUse Write "$N" s7)"
# large legacy file may grow within its band (warn), blocks only when crossing up
BIG="$SCRATCH/big.py"; { for i in $(seq 1 90); do genlong "f$i" 8; done; } > "$BIG"   # ~900 lines, high band
expect "pre big legacy"                     0 $H "$(ev PreToolUse Edit "$BIG" s8)"
{ cat "$BIG"; genlong extra 8; } > "$BIG.tmp" && mv "$BIG.tmp" "$BIG"
expect "legacy 900-line file +10 lines -> warn only" 0 $H "$(ev PostToolUse Edit "$BIG" s8)"
expect_out "large legacy file notice"       'large legacy file'
{ cat "$BIG"; for i in $(seq 1 70); do genlong "g$i" 8; done; } > "$BIG.tmp" && mv "$BIG.tmp" "$BIG"   # -> ~1610 very-high
expect "legacy file crosses into very-high -> block" 2 $H "$(ev PostToolUse Edit "$BIG" s8)"
# moving a legacy high-CCN function into a new module is not "new"
A="$SCRATCH/mod_a.py"; B="$SCRATCH/mod_b.py"; gen legacy_move 30 > "$A"
expect "pre a (legacy CCN 31)"              0 $H "$(ev PreToolUse Edit "$A" s9)"
expect "pre b (new file)"                   0 $H "$(ev PreToolUse Write "$B" s9)"
gen legacy_move 30 > "$B"; printf 'from mod_b import legacy_move\n' > "$A"
expect "moved fn in b -> warn, not block"   0 $H "$(ev PostToolUse Write "$B" s9)"
gen legacy_move 33 > "$B"
expect "moved AND grown -> block"           2 $H "$(ev PostToolUse Write "$B" s9)"
# ignore matches components, not substrings
mkdir -p "$SCRATCH/builder" "$SCRATCH/build" "$SCRATCH/app/migrations"
gen x 40 > "$SCRATCH/builder/x.py"; gen x 40 > "$SCRATCH/build/x.py"; gen x 40 > "$SCRATCH/app/migrations/0001.py"
expect "builder/ is NOT ignored -> block"   2 $H "$(ev PostToolUse Write "$SCRATCH/builder/x.py" s10)"
expect "build/ ignored -> 0"                0 $H "$(ev PostToolUse Write "$SCRATCH/build/x.py" s10)"
expect "migrations/ ignored by default -> 0" 0 $H "$(ev PostToolUse Write "$SCRATCH/app/migrations/0001.py" s10)"
# formatter presence: NLOC measured on the formatted shape at both Pre and Post
mkdir -p "$SCRATCH/fmt"; printf 'line-length = 100\n' > "$SCRATCH/fmt/ruff.toml"
F2="$SCRATCH/fmt/one.py"; { echo "def legacy(x):"; for i in $(seq 1 105); do echo "    if x == $i: return $i"; done; echo "    return 0"; } > "$F2"   # 107 NLOC unformatted; ruff format expands to ~212
expect "pre (unformatted 107 lines)"        0 $H "$(ev PreToolUse Edit "$F2" s11)"
{ echo "# touched"; cat "$F2"; } > "$F2.tmp" && mv "$F2.tmp" "$F2"
expect "comment added; formatter would double NLOC -> no false 'made it worse'" 0 $H "$(ev PostToolUse Edit "$F2" s11)"
# bad config never crashes the ward
mkdir -p "$SCRATCH/badcfg/.wards"; printf '[complexity]\nccn = 10\nlength = [1, 2]\n' > "$SCRATCH/badcfg/.wards/config.toml"
gen fine 3 > "$SCRATCH/badcfg/m.py"
expect "malformed thresholds -> defaults, exit 0" 0 $H "$(ev PostToolUse Write "$SCRATCH/badcfg/m.py" s12)"
check "malformed thresholds audited"        grep -q "not three ascending ints" "$WARDS_AUDIT_LOG"
gen bad 25 > "$SCRATCH/badcfg/m.py"
expect "…and defaults still enforce"        2 $H "$(ev PostToolUse Write "$SCRATCH/badcfg/m.py" s12)"
WARDS_COMPLEXITY_IGNORE="node_modules,generated" expect "env list override as CSV works" 2 $H "$(ev PostToolUse Write "$SCRATCH/badcfg/m.py" s12)"
mkdir -p "$SCRATCH/badcfg/generated"; gen bad 25 > "$SCRATCH/badcfg/generated/g.py"
WARDS_COMPLEXITY_IGNORE="node_modules,generated" expect "env CSV ignore applies" 0 $H "$(ev PostToolUse Write "$SCRATCH/badcfg/generated/g.py" s12)"

echo "# missing lizard"
PATH="/usr/bin:/bin" expect "no lizard -> exit 0 + INACTIVE"  0 $H "$(ev PostToolUse Write "$N" s2)"
expect_out "inactive message"            'INACTIVE'
finish
