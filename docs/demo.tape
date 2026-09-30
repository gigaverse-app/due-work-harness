# Records docs/demo.gif with https://github.com/charmbracelet/vhs. From the repo
# root on Linux or WSL, with uv, git and a PostgreSQL reachable through the PG*
# variables (as CI's demos job has):
#
#   PGHOST=localhost PGPORT=5432 PGUSER=postgres PGPASSWORD=postgres vhs docs/demo.tape
#
# Everything shown is real output: the harness running against the demos in
# demos/, their upstream applications cloned at the commits
# demos/fetch_upstream.py pins. The recording runs in a fresh clone of HEAD at
# a temporary directory, so no local path appears, in the environment
# docs/demo_env.sh sets. demos/test_readme_gif.py replays every command typed
# below and asserts the lines the GIF relies on, so the GIF cannot claim output
# the harness no longer prints.

Output docs/demo.gif

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 15
Set LineHeight 1.15
Set Width 1480
Set Height 640
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 22ms
Set CursorBlink false

Hide
Type "recording_dir=$(mktemp -d /tmp/due-work-harness-recording.XXXXXX) && git clone -q . $recording_dir && cd $recording_dir"
Enter
Type "uv run -q --no-project python demos/fetch_upstream.py procrastinate dbos-demo-apps saleor >/dev/null && uv sync -q --locked --all-extras --group demos && uv pip install -q -e demos/.upstream/procrastinate && echo SET''UP-DONE"
Enter
Wait+Screen@900s /SETUP-DONE/
Type "source .venv/bin/activate && source docs/demo_env.sh && export PS1='$ ' && clear"
Enter
Sleep 1s
Show

Type@8ms "# procrastinate's own Django demo, unmodified. Kill the worker after every commit of 'create book':"
Enter
Type "pytest demos/procrastinate_demo_django -k 'Shipped and handoff' --xfail-tb -q"
Enter
Wait+Screen /xfailed/
Sleep 4s

Hide
Type "clear"
Enter
Show
Type@8ms "# The demo's fix, ATOMIC_REQUESTS = True: the same proof, every history converges."
Enter
Type "pytest demos/procrastinate_demo_django -k 'Fixes and handoff' -v"
Enter
Wait+Screen /passed/
Sleep 2500ms

Hide
Type "clear"
Enter
Show
Type@8ms "# DBOS's transactional-outbox demo: the process dies right after the notification is sent."
Enter
Type "pytest demos/dbos_transactional_outbox -p no:django -k C-known_gap --xfail-tb -q"
Enter
Wait+Screen@60s /xfailed/
Sleep 4s

Hide
Type "clear"
Enter
Show
Type@8ms "# And statically: every handoff in Saleor, found and accounted for."
Enter
Type "due-work-harness check --root demos/saleor_checkout"
Enter
Wait+Screen /handoff site/
Sleep 3500ms
