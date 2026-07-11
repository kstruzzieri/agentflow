# OpenHands Hook Integration pack

This pack declares a hook template (`hooks/pre-commit.sh`) and this README by
relative path. Agentflow validates that the paths are safe relative paths but
never reads, stats, or executes them. To use the hook, copy it into your host
project and register it with your runtime yourself.
