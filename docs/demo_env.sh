# The shell environment docs/demo.tape records docs/demo.gif in, and
# demos/test_readme_gif.py replays its commands in. Source it from the repo root
# with the project's virtualenv active and PostgreSQL reachable through the PG*
# variables (host, port, user, password), as CI's demos job has.

# The procrastinate demo's database and settings, as CI's demos job passes them.
export PGDATABASE=procrastinate_demo
export DJANGO_SETTINGS_MODULE=demos.procrastinate_demo_django.settings
# Presentation only: short tracebacks, no captured logs, no session header.
export PYTEST_ADDOPTS="--tb=short --show-capture=no --no-header"
