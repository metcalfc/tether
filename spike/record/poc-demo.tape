# poc-demo.tape -- the microsandbox + nono + tether PoC demo
# (design/tether-poc.md, "Demo script"), driven by spike/msb/demo.sh.
#
#   vhs spike/record/poc-demo.tape    # from the repo root -> overnight/run/poc-demo.mp4
#
# Needs VHS 0.8+ (Wait), msb in ~/.microsandbox, the Landlock libkrunfw in
# overnight/run/krunfw-landlock, and the keychain items tether-anthropic and
# tether-gh-scratch. tether reads both through `security` itself: no key is
# typed, shown or put in the guest. Step 4 runs Claude Code on its default
# model; every other model call is haiku. About $0.5 in all.

Output overnight/run/poc-demo.mp4

Set Shell "bash"
Set FontSize 15
Set Width 1800
Set Height 1040
Set Padding 20
Set TypingSpeed 30ms
Set WindowBar Colorful

# ---- off camera: fresh tether state, compile, gateway up
Hide
Type "export PS1='host$ ' && unset ANTHROPIC_API_KEY && clear" Enter
Type "spike/msb/demo.sh setup >/dev/null 2>&1; clear; echo READY-$((1+1))" Enter
Wait+Screen@120s /READY-2/
Type "clear" Enter
Sleep 500ms
Show

Type "spike/msb/demo.sh 1" Enter
Wait+Screen@120s /step 1 done/
Sleep 8s

Type "clear && spike/msb/demo.sh 2" Enter
Wait+Screen@300s /step 2 done/
Sleep 4s

Type "clear && spike/msb/demo.sh 3" Enter
Wait+Screen@300s /step 3 done/
Sleep 6s

Type "clear && spike/msb/demo.sh 4" Enter
Wait+Screen@720s /step 4 done/
Sleep 8s

Type "clear && spike/msb/demo.sh 5" Enter
Wait+Screen@60s /step 5 done/
Sleep 7s

Type "clear && spike/msb/demo.sh 6" Enter
Wait+Screen@300s /step 6 done/
Sleep 6s

Type "clear && spike/msb/demo.sh 7" Enter
Wait+Screen@300s /step 7 done/
Sleep 8s

Type "clear && spike/msb/demo.sh 8" Enter
Wait+Screen@600s /step 8 done/
Sleep 10s

Type "clear && spike/msb/demo.sh 9" Enter
Wait+Screen@300s /step 9 done/
Sleep 7s

Type "clear && spike/msb/demo.sh 10" Enter
Wait+Screen@120s /step 10 done/
Sleep 4s

Type "clear && spike/msb/demo.sh 5" Enter
Wait+Screen@60s /step 5 done/
Sleep 8s

Hide
Type "spike/msb/demo.sh teardown" Enter
Wait+Screen@120s /step teardown done/
